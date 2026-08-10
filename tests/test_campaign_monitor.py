"""Tests for the proactive campaign-health monitor
(``backend.app.services.campaign_monitor``).

Uses an in-memory sync SQLite engine (same trick as
``test_manager_anomaly_detector.py``) seeded with a tenant + campaign +
``campaign_recipients``/``campaign_events``/``outreach_members``/
``email_sends`` rows.

Note on the CHECK-constraint caveat in the spec: ``ck_manager_alerts_kind``
is only created by the Alembic migration (raw ``op.create_check_constraint``
in ``aa01b2c3d4e5_manager_view_overhaul.py`` / ``cmp_001_campaign_alert_
kinds.py``) — it is NOT part of ``ManagerAlert.__table_args__`` in
``models.py``, so ``Base.metadata.create_all`` here does not include it at
all. Inserting the new ``campaign_*`` kinds is therefore unconstrained
under this SQLite fixture, exactly like the existing CS/Support kinds in
``test_manager_anomaly_detector.py`` already are. No special handling was
needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def sync_session():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers mapped classes

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ── Seed helpers ───────────────────────────────────────────────────────


def _make_tenant(session, name="Acme"):
    from backend.app.models import Tenant

    tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}")
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant


def _outreach_config():
    """Minimal valid OutreachConfig, UTC send window for deterministic
    quota-window-boundary math in tests."""
    from backend.app.services.outreach.common import (
        OutreachConfig,
        OutreachTemplate,
        SendWindow,
    )

    template = OutreachTemplate(
        subject="hi", body="hi", sender_name="A", sender_business="B",
        physical_address="123 Main St",
    )
    cfg = OutreachConfig(template=template, send_window=SendWindow(timezone="UTC"))
    return cfg.model_dump(mode="json")


def _make_campaign(
    session, tenant, *, kind="external", status="active", name="Spring Blast", **kwargs
):
    from backend.app.models import Campaign

    campaign = Campaign(
        tenant_id=tenant.id, name=name, channel="email", kind=kind, status=status, **kwargs
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


def _add_recipients(session, campaign, tenant, n):
    from backend.app.models import CampaignRecipient

    for i in range(n):
        session.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                tenant_id=tenant.id,
                email_address=f"p{i}-{uuid.uuid4().hex[:6]}@example.com",
            )
        )
    session.commit()


def _add_events(session, campaign, tenant, event_type, n, when=None):
    from backend.app.models import CampaignEvent

    when = when or datetime.now(timezone.utc)
    for _ in range(n):
        session.add(
            CampaignEvent(
                campaign_id=campaign.id,
                tenant_id=tenant.id,
                event_type=event_type,
                occurred_at=when,
            )
        )
    session.commit()


def _add_members(session, campaign, tenant, state, n):
    from backend.app.models import Customer, OutreachMember

    rows = []
    for i in range(n):
        customer = Customer(tenant_id=tenant.id, name=f"Prospect {i}-{uuid.uuid4().hex[:4]}")
        session.add(customer)
        session.flush()
        member = OutreachMember(
            tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id, state=state
        )
        session.add(member)
        rows.append(member)
    session.commit()
    return rows


def _add_email_send(session, campaign, tenant, *, when=None, status="sent"):
    from backend.app.models import EmailSend

    when = when or datetime.now(timezone.utc)
    send = EmailSend(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        provider="google",
        to_address="x@example.com",
        subject="hi",
        body="hi",
        status=status,
        created_at=when,
    )
    session.add(send)
    session.commit()
    return send


class _StubLLMResponse:
    def __init__(self, text):
        self.text = text


class _StubRouter:
    """Sync stub for ``get_router()`` — mirrors the ``.invoke`` shape
    ``campaign_monitor._render_haiku`` calls."""

    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc

    def invoke(self, req):
        if self._exc is not None:
            raise self._exc
        return _StubLLMResponse(self._result)


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    """No test may hit the real router: default every test to a raising
    stub (exercising the deterministic template fallback deliberately).
    Tests about rendering override ``get_router`` themselves — their
    later monkeypatch.setattr wins."""
    from backend.app.services import campaign_monitor

    monkeypatch.setattr(
        campaign_monitor,
        "get_router",
        lambda: _StubRouter(exc=RuntimeError("live LLM calls are disabled in tests")),
    )


def _frozen_datetime(fixed):
    """A ``datetime`` subclass whose ``.now()`` always returns ``fixed``,
    for monkeypatching ``campaign_monitor.datetime`` in tests that need a
    deterministic wall clock (the quota-starved window-age gate)."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    return _Frozen


# ── Bounce spike ─────────────────────────────────────────────────────


def test_bounce_spike_below_threshold_noop(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 1)  # 4% < 5%

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted if a.kind == "campaign_bounce_spike"] == []


def test_bounce_spike_fires_above_threshold(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 5)  # 20% >= 5%

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_bounce_spike"]
    assert len(alerts) == 1
    assert alerts[0].severity == "high"
    assert alerts[0].evidence["bounces"] == 5
    assert alerts[0].evidence["sent"] == 25


# ── Opt-out spike ────────────────────────────────────────────────────


def test_optout_spike_below_threshold_noop(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 25)
    # 0 unsubscribes, 0 opted-out members.

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted if a.kind == "campaign_optout_spike"] == []


def test_optout_spike_fires_on_unsubscribe_events_external(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="external")
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "unsubscribe", 1)  # 4% >= 2%

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_optout_spike"]
    assert len(alerts) == 1
    assert alerts[0].severity == "high"
    assert alerts[0].evidence["unsubscribes"] == 1
    assert alerts[0].evidence["opted_out_members"] == 0


def test_optout_spike_counts_opted_out_members_for_outreach(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_members(sync_session, campaign, tenant, "opted_out", 1)  # 4% >= 2%

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_optout_spike"]
    assert len(alerts) == 1
    assert alerts[0].evidence["opted_out_members"] == 1


# ── No-engagement ────────────────────────────────────────────────────


def test_no_engagement_below_sent_threshold_noop(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 10)  # < 30

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted if a.kind == "campaign_no_engagement"] == []


def test_no_engagement_fires_when_open_rate_low_external(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="external")
    _add_recipients(sync_session, campaign, tenant, 30)
    _add_events(sync_session, campaign, tenant, "open", 2)  # 6.7% < 10%

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_no_engagement"]
    assert len(alerts) == 1
    assert alerts[0].severity == "medium"
    assert alerts[0].evidence["replies"] == 0


def test_no_engagement_skipped_when_open_rate_high_external(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="external")
    _add_recipients(sync_session, campaign, tenant, 30)
    _add_events(sync_session, campaign, tenant, "open", 6)  # 20% >= 10%: real engagement

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted if a.kind == "campaign_no_engagement"] == []


def test_no_engagement_fires_for_outreach_regardless_of_opens(sync_session):
    """Outreach skips the open-rate qualifier entirely — replies are the
    outreach engine's primary engagement signal."""
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    _add_recipients(sync_session, campaign, tenant, 30)
    _add_events(sync_session, campaign, tenant, "open", 20)  # high open rate, still no reply

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_no_engagement"]
    assert len(alerts) == 1
    assert alerts[0].evidence["open_rate"] is None


def test_no_engagement_noop_when_there_are_replies(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="external")
    _add_recipients(sync_session, campaign, tenant, 30)
    _add_events(sync_session, campaign, tenant, "reply", 1)

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted if a.kind == "campaign_no_engagement"] == []


# ── Stalled (outreach only) ──────────────────────────────────────────


def test_stalled_fires_for_outreach_with_pending_members_and_no_recent_sends(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    _add_members(sync_session, campaign, tenant, "queued", 3)

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_stalled"]
    assert len(alerts) == 1
    assert alerts[0].severity == "medium"
    assert alerts[0].evidence["pending_members"] == 3


def test_stalled_noop_when_no_pending_members(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    _add_members(sync_session, campaign, tenant, "completed", 3)  # all terminal

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted if a.kind == "campaign_stalled"] == []


def test_stalled_noop_when_recent_send_exists(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    _add_members(sync_session, campaign, tenant, "queued", 3)
    _add_email_send(sync_session, campaign, tenant, when=datetime.now(timezone.utc) - timedelta(hours=2))

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted if a.kind == "campaign_stalled"] == []


# ── Quota starved (outreach only) — direct detector unit tests ───────
#
# Tested against ``_detect_quota_starved`` directly (it takes ``now`` as
# an explicit argument) rather than through the full ``scan_tenant`` path,
# to make the window-age gate deterministic without monkeypatching the
# wall clock for every assertion.


def test_quota_starved_fires_when_window_old_and_tenant_cap_exhausted(sync_session, monkeypatch):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)  # 10h into the UTC day
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    other = _make_campaign(
        sync_session, tenant, kind="outreach", name="Other", config=_outreach_config()
    )
    for _ in range(3):
        _add_email_send(sync_session, other, tenant, when=now - timedelta(hours=1))

    class _StubSettings:
        OUTREACH_TENANT_DAILY_SEND_CAP = 3

    monkeypatch.setattr(campaign_monitor, "get_settings", lambda: _StubSettings())

    result = campaign_monitor._detect_quota_starved(sync_session, tenant, campaign, {}, 0, now)
    assert result is not None
    assert result.kind == "campaign_quota_starved"
    assert result.severity == "low"
    assert result.evidence["tenant_sent_today"] == 3
    assert result.evidence["campaign_sent_today"] == 0


def test_quota_starved_noop_when_window_just_started(sync_session, monkeypatch):
    """Pragmatic '2 consecutive scans' encoding: a campaign whose local
    quota day started less than one scan interval (1h) ago never fires,
    even if the tenant cap is already exhausted."""
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    just_started = datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)  # 30 min into the day
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    other = _make_campaign(
        sync_session, tenant, kind="outreach", name="Other", config=_outreach_config()
    )
    for _ in range(3):
        _add_email_send(sync_session, other, tenant, when=just_started)

    class _StubSettings:
        OUTREACH_TENANT_DAILY_SEND_CAP = 3

    monkeypatch.setattr(campaign_monitor, "get_settings", lambda: _StubSettings())

    result = campaign_monitor._detect_quota_starved(
        sync_session, tenant, campaign, {}, 0, just_started
    )
    assert result is None


def test_quota_starved_noop_when_campaign_itself_sent_today(sync_session, monkeypatch):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    _add_email_send(sync_session, campaign, tenant, when=now - timedelta(hours=1))

    class _StubSettings:
        OUTREACH_TENANT_DAILY_SEND_CAP = 1

    monkeypatch.setattr(campaign_monitor, "get_settings", lambda: _StubSettings())

    result = campaign_monitor._detect_quota_starved(sync_session, tenant, campaign, {}, 0, now)
    assert result is None


def test_quota_starved_integration_through_scan_tenant(sync_session, monkeypatch):
    """End-to-end wiring check: the detector actually fires through the
    full scan path (dedup, insert, Haiku render), not just in isolation."""
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    fixed_now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
    campaign = _make_campaign(sync_session, tenant, kind="outreach", config=_outreach_config())
    other = _make_campaign(
        sync_session, tenant, kind="outreach", name="Other", config=_outreach_config()
    )
    for _ in range(3):
        _add_email_send(sync_session, other, tenant, when=fixed_now - timedelta(hours=1))

    class _StubSettings:
        OUTREACH_TENANT_DAILY_SEND_CAP = 3

    monkeypatch.setattr(campaign_monitor, "get_settings", lambda: _StubSettings())
    monkeypatch.setattr(campaign_monitor, "datetime", _frozen_datetime(fixed_now))

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_quota_starved"]
    assert len(alerts) == 1


# ── Outreach-only detectors skip external campaigns ──────────────────


def test_outreach_only_detectors_skip_external_campaigns(sync_session, monkeypatch):
    """External campaigns still trigger bounce/no-engagement detectors,
    but never the outreach-only ones (no outreach_members/email_sends
    exist for them anyway, but this asserts the kind guard too)."""
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant, kind="external")
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 5)  # fires bounce spike

    class _StubSettings:
        OUTREACH_TENANT_DAILY_SEND_CAP = 1

    monkeypatch.setattr(campaign_monitor, "get_settings", lambda: _StubSettings())

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    kinds = {a.kind for a in inserted}
    assert "campaign_bounce_spike" in kinds
    assert "campaign_stalled" not in kinds
    assert "campaign_quota_starved" not in kinds


# ── Fingerprint idempotency ───────────────────────────────────────────


def test_scan_twice_yields_exactly_one_active_alert(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 5)

    first = campaign_monitor.scan_tenant(sync_session, tenant)
    assert len([a for a in first if a.kind == "campaign_bounce_spike"]) == 1

    second = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in second if a.kind == "campaign_bounce_spike"] == []

    from backend.app.models import ManagerAlert

    active = (
        sync_session.query(ManagerAlert)
        .filter(
            ManagerAlert.kind == "campaign_bounce_spike",
            ManagerAlert.resolved_at.is_(None),
        )
        .all()
    )
    assert len(active) == 1


# ── Condition-cleared resolution + re-fire ───────────────────────────


def test_condition_cleared_resolves_alert_and_refire_allowed(sync_session):
    from backend.app.services import campaign_monitor
    from backend.app.models import ManagerAlert

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 5)  # 20% -> fires

    first = campaign_monitor.scan_tenant(sync_session, tenant)
    original = [a for a in first if a.kind == "campaign_bounce_spike"][0]

    # Dilute the bounce rate under threshold: sent goes to 225, bounces
    # stay at 5 -> ~2.2%.
    _add_recipients(sync_session, campaign, tenant, 200)
    campaign_monitor.scan_tenant(sync_session, tenant)

    sync_session.refresh(original)
    assert original.resolved_at is not None

    # Re-fire allowed once the fingerprint slot is free: push bounces
    # back over 5%.
    _add_events(sync_session, campaign, tenant, "bounce", 20)  # 25/225 ~= 11.1%
    refired = campaign_monitor.scan_tenant(sync_session, tenant)
    refired_alerts = [a for a in refired if a.kind == "campaign_bounce_spike"]
    assert len(refired_alerts) == 1
    assert refired_alerts[0].id != original.id

    all_bounce_alerts = (
        sync_session.query(ManagerAlert).filter(ManagerAlert.kind == "campaign_bounce_spike").all()
    )
    assert len(all_bounce_alerts) == 2


# ── External-campaign recency gate ────────────────────────────────────
#
# External campaigns never leave status='active' (models.py:2446), so
# without the recency gate the first production scan would alert on
# every historical external campaign ever ingested — and those alerts
# could never auto-resolve.


def test_stale_external_campaign_is_not_scanned(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    now = datetime.now(timezone.utc)
    stale = _make_campaign(
        sync_session, tenant, started_at=now - timedelta(days=90)
    )
    _add_recipients(sync_session, stale, tenant, 25)
    _add_events(sync_session, stale, tenant, "bounce", 5)  # 20% — would fire

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert inserted == []


def test_recent_external_campaign_still_scanned(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    now = datetime.now(timezone.utc)
    fresh = _make_campaign(
        sync_session, tenant, started_at=now - timedelta(days=3)
    )
    _add_recipients(sync_session, fresh, tenant, 25)
    _add_events(sync_session, fresh, tenant, "bounce", 5)

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a.kind for a in inserted] == ["campaign_bounce_spike"]


def test_external_alert_resolves_when_campaign_ages_out(sync_session):
    """When an external campaign ages out of the window — even if it was
    the tenant's ONLY candidate (fast-skip path) — its open alerts must
    resolve rather than stay open forever."""
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    now = datetime.now(timezone.utc)
    campaign = _make_campaign(
        sync_session, tenant, started_at=now - timedelta(days=3)
    )
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 5)

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alert = [a for a in inserted if a.kind == "campaign_bounce_spike"][0]

    campaign.started_at = now - timedelta(days=90)
    sync_session.commit()

    # The tenant now has zero candidate campaigns, so this exercises the
    # fast-skip branch's resolve pass.
    assert campaign_monitor.scan_tenant(sync_session, tenant) == []
    sync_session.refresh(alert)
    assert alert.resolved_at is not None


# ── Completion wrap-up report ─────────────────────────────────────────


def test_completion_report_written_once_and_alert_emitted_once(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    now = datetime.now(timezone.utc)
    campaign = _make_campaign(
        sync_session, tenant, kind="external", status="completed",
        ended_at=now - timedelta(hours=1),
    )
    _add_recipients(sync_session, campaign, tenant, 40)
    _add_events(sync_session, campaign, tenant, "reply", 4)
    _add_events(sync_session, campaign, tenant, "open", 10)

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    summaries = [a for a in inserted if a.kind == "campaign_completed_summary"]
    assert len(summaries) == 1
    assert summaries[0].severity == "low"

    sync_session.refresh(campaign)
    assert "completion_report" in (campaign.insights or {})
    report = campaign.insights["completion_report"]
    assert report["rollup"]["sent"] == 40
    assert report["rollup"]["replies"] == 4
    assert "narrative" in report

    # Second scan: idempotent by the insights-key check, no new alert.
    inserted_again = campaign_monitor.scan_tenant(sync_session, tenant)
    assert [a for a in inserted_again if a.kind == "campaign_completed_summary"] == []


def test_completion_report_includes_funnel_for_outreach(sync_session):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    now = datetime.now(timezone.utc)
    campaign = _make_campaign(
        sync_session, tenant, kind="outreach", status="completed",
        ended_at=now - timedelta(hours=1), config=_outreach_config(),
    )
    _add_recipients(sync_session, campaign, tenant, 10)
    _add_events(sync_session, campaign, tenant, "reply", 1)
    _add_members(sync_session, campaign, tenant, "replied", 1)
    _add_members(sync_session, campaign, tenant, "completed", 9)

    campaign_monitor.scan_tenant(sync_session, tenant)

    sync_session.refresh(campaign)
    report = campaign.insights["completion_report"]
    assert report["funnel"] == {"replied": 1, "completed": 9}


# ── LLM-failure path ───────────────────────────────────────────────────


def test_llm_failure_inserts_alert_with_template_body(sync_session, monkeypatch):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 5)

    monkeypatch.setattr(
        campaign_monitor, "get_router", lambda: _StubRouter(exc=RuntimeError("provider blip"))
    )

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alerts = [a for a in inserted if a.kind == "campaign_bounce_spike"]
    assert len(alerts) == 1
    alert = alerts[0]
    # Template fallback (deterministic, evidence-based) stayed in place.
    assert "bounce rate hit" in alert.title.lower()
    assert "Suggestion:" in alert.body


def test_llm_success_overwrites_template_copy(sync_session, monkeypatch):
    from backend.app.services import campaign_monitor

    tenant = _make_tenant(sync_session)
    campaign = _make_campaign(sync_session, tenant)
    _add_recipients(sync_session, campaign, tenant, 25)
    _add_events(sync_session, campaign, tenant, "bounce", 5)

    payload = (
        '{"title": "Bounces spiked on Spring Blast", '
        '"body": "5 of 25 sends bounced. Suggestion: pause and re-verify the list."}'
    )
    monkeypatch.setattr(campaign_monitor, "get_router", lambda: _StubRouter(result=payload))

    inserted = campaign_monitor.scan_tenant(sync_session, tenant)
    alert = [a for a in inserted if a.kind == "campaign_bounce_spike"][0]
    assert alert.title == "Bounces spiked on Spring Blast"
    assert "Suggestion:" in alert.body


# ── Tenant isolation ───────────────────────────────────────────────────


def test_two_tenant_isolation_one_failure_does_not_block_the_other(sync_session, monkeypatch):
    from backend.app.services import campaign_monitor
    from backend.app.models import ManagerAlert

    tenant_a = _make_tenant(sync_session, "Acme")
    tenant_b = _make_tenant(sync_session, "Globex")

    campaign_a = _make_campaign(sync_session, tenant_a)
    _add_recipients(sync_session, campaign_a, tenant_a, 25)
    _add_events(sync_session, campaign_a, tenant_a, "bounce", 5)

    campaign_b = _make_campaign(sync_session, tenant_b)
    _add_recipients(sync_session, campaign_b, tenant_b, 25)
    _add_events(sync_session, campaign_b, tenant_b, "bounce", 5)

    real_scan_tenant = campaign_monitor.scan_tenant

    def _boom_for_a(session, tenant):
        if tenant.id == tenant_a.id:
            raise RuntimeError("boom")
        return real_scan_tenant(session, tenant)

    monkeypatch.setattr(campaign_monitor, "scan_tenant", _boom_for_a)

    result = campaign_monitor.scan_all_tenants(sync_session)
    assert result["by_tenant"][str(tenant_a.id)] == -1
    assert result["by_tenant"][str(tenant_b.id)] == 1

    alerts_a = sync_session.query(ManagerAlert).filter(ManagerAlert.tenant_id == tenant_a.id).all()
    alerts_b = sync_session.query(ManagerAlert).filter(ManagerAlert.tenant_id == tenant_b.id).all()
    assert alerts_a == []
    assert len(alerts_b) == 1
    assert alerts_b[0].kind == "campaign_bounce_spike"


# ── Kind vocabulary guard ───────────────────────────────────────────────


def test_all_detector_and_completion_kinds_are_in_the_exported_constant():
    from backend.app.services import campaign_monitor

    emitted = set(campaign_monitor._DETECTOR_BY_KIND.keys()) | {"campaign_completed_summary"}
    assert emitted == set(campaign_monitor.CAMPAIGN_ALERT_KINDS)


def test_campaign_alert_kinds_are_a_subset_of_manager_alert_kinds():
    """Cross-check against the (separately fable-authored) full
    vocabulary tuple the migration mirrors — models.py already declares
    it alongside migration cmp_001."""
    from backend.app.models import MANAGER_ALERT_KINDS
    from backend.app.services.campaign_monitor import CAMPAIGN_ALERT_KINDS

    assert set(CAMPAIGN_ALERT_KINDS).issubset(set(MANAGER_ALERT_KINDS))
