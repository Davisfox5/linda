"""Tests for the Slack notification + alert-fanout layer.

Covers:

* ``NotificationService.post_to_slack_channel`` — payload shape +
  Authorization header, non-ok response handling, network failure
  swallowed (never raises).

* ``manager_alert_fanout.fanout`` — in-app notifications get inserted
  for managers + admins, Slack fires only when an integration exists
  AND severity ≥ ``slack_min_severity``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
import respx
from httpx import Response
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
    import backend.app.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ── post_to_slack_channel ──────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_post_to_slack_channel_sends_authorized_payload():
    from backend.app.services.notification_service import NotificationService

    route = respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=Response(200, json={"ok": True, "ts": "1.0"})
    )
    svc = NotificationService()
    out = await svc.post_to_slack_channel(
        bot_token="xoxb-abc",
        channel_id="C123",
        text="Refund mentions jumped 6x.",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
    )
    assert out == {"ok": True, "ts": "1.0"}
    assert route.called
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer xoxb-abc"
    body = req.read()
    assert b'"channel":"C123"' in body
    assert b'"blocks"' in body


@pytest.mark.asyncio
@respx.mock
async def test_post_to_slack_channel_handles_non_ok():
    from backend.app.services.notification_service import NotificationService

    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    svc = NotificationService()
    out = await svc.post_to_slack_channel(
        bot_token="xoxb-abc", channel_id="C999", text="hi"
    )
    assert out["ok"] is False
    assert out["error"] == "channel_not_found"


@pytest.mark.asyncio
@respx.mock
async def test_post_to_slack_channel_swallows_network_failure():
    from backend.app.services.notification_service import NotificationService

    respx.post("https://slack.com/api/chat.postMessage").mock(
        side_effect=ConnectionError("DNS fail")
    )
    svc = NotificationService()
    out = await svc.post_to_slack_channel(
        bot_token="xoxb-abc", channel_id="C123", text="hi"
    )
    assert out["ok"] is False
    assert "error" in out


# ── fanout ─────────────────────────────────────────────────────────────


def _seed_tenant_with_recipients(session: Session):
    from backend.app.models import Tenant, User

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    session.add(tenant)
    session.flush()
    manager = User(
        tenant_id=tenant.id,
        email=f"m-{uuid.uuid4().hex[:6]}@acme.test",
        name="Mgr",
        role="manager",
    )
    admin = User(
        tenant_id=tenant.id,
        email=f"a-{uuid.uuid4().hex[:6]}@acme.test",
        name="Adm",
        role="admin",
    )
    agent = User(
        tenant_id=tenant.id,
        email=f"r-{uuid.uuid4().hex[:6]}@acme.test",
        name="Rep",
        role="agent",
    )
    session.add_all([manager, admin, agent])
    session.commit()
    return tenant, manager, admin, agent


def test_fanout_inserts_inapp_for_managers_and_admins_only(sync_session):
    from backend.app.models import AlertChannelConfig, ManagerAlert, Notification
    from backend.app.services.manager_alert_fanout import fanout

    tenant, manager, admin, agent = _seed_tenant_with_recipients(sync_session)
    sync_session.add(AlertChannelConfig(tenant_id=tenant.id))
    alert = ManagerAlert(
        tenant_id=tenant.id,
        kind="topic_spike",
        severity="high",
        title="Refund mentions jumped 6x in 48 hours.",
        body="Baseline was 1/day; recent 12 calls.",
        evidence={"topic": "refund_request"},
        fingerprint="fp-1",
    )
    sync_session.add(alert)
    sync_session.commit()
    sync_session.refresh(alert)

    fanout(sync_session, [alert])
    sync_session.commit()

    notifs = sync_session.query(Notification).all()
    by_user = {str(n.user_id) for n in notifs}
    assert str(manager.id) in by_user
    assert str(admin.id) in by_user
    assert str(agent.id) not in by_user
    assert all(n.kind == "manager_alert" for n in notifs)


def test_fanout_skips_slack_when_severity_below_threshold(sync_session):
    """slack_min_severity='medium' + alert.severity='low' → no Slack post."""
    from backend.app.models import (
        AlertChannelConfig,
        ManagerAlert,
        SlackIntegration,
    )
    from backend.app.services.manager_alert_fanout import fanout
    from backend.app.services.token_crypto import encrypt_token

    tenant, _, _, _ = _seed_tenant_with_recipients(sync_session)
    sync_session.add(
        AlertChannelConfig(
            tenant_id=tenant.id, slack_enabled=True, slack_min_severity="medium"
        )
    )
    sync_session.add(
        SlackIntegration(
            tenant_id=tenant.id,
            slack_team_id="T123",
            bot_token_encrypted=encrypt_token("xoxb-test") or "",
            default_channel_id="C123",
        )
    )
    alert = ManagerAlert(
        tenant_id=tenant.id,
        kind="topic_spike",
        severity="low",
        title="Low-severity blip.",
        evidence={},
        fingerprint="fp-low",
    )
    sync_session.add(alert)
    sync_session.commit()

    posted: List[Dict[str, Any]] = []

    class _StubService:
        async def post_to_slack_channel(self, **kwargs):
            posted.append(kwargs)
            return {"ok": True}

    with patch(
        "backend.app.services.manager_alert_fanout.NotificationService",
        return_value=_StubService(),
    ):
        fanout(sync_session, [alert])

    assert posted == [], "low severity should not have been sent to Slack"


def test_fanout_sends_slack_for_high_severity(sync_session):
    from backend.app.models import (
        AlertChannelConfig,
        ManagerAlert,
        SlackIntegration,
    )
    from backend.app.services.manager_alert_fanout import fanout
    from backend.app.services.token_crypto import encrypt_token

    tenant, _, _, _ = _seed_tenant_with_recipients(sync_session)
    sync_session.add(
        AlertChannelConfig(
            tenant_id=tenant.id, slack_enabled=True, slack_min_severity="medium"
        )
    )
    sync_session.add(
        SlackIntegration(
            tenant_id=tenant.id,
            slack_team_id="T123",
            bot_token_encrypted=encrypt_token("xoxb-test") or "",
            default_channel_id="C123",
            default_channel_name="alerts",
        )
    )
    alert = ManagerAlert(
        tenant_id=tenant.id,
        kind="churn_surge",
        severity="high",
        title="Five high-risk calls in 24 hours.",
        evidence={"current_count": 5},
        fingerprint="fp-high",
    )
    sync_session.add(alert)
    sync_session.commit()

    posted: List[Dict[str, Any]] = []

    class _StubService:
        async def post_to_slack_channel(self, **kwargs):
            posted.append(kwargs)
            return {"ok": True}

    with patch(
        "backend.app.services.manager_alert_fanout.NotificationService",
        return_value=_StubService(),
    ):
        fanout(sync_session, [alert])

    assert len(posted) == 1
    payload = posted[0]
    assert payload["channel_id"] == "C123"
    assert payload["bot_token"] == "xoxb-test"
    assert "Five high-risk calls" in payload["text"]
    # Severity-coded block with the alert kind in context.
    blocks_text = str(payload["blocks"])
    assert "churn_surge" in blocks_text


# ── manager_alert.created webhook enqueue ───────────────────────────────


def test_fanout_enqueues_one_webhook_per_alert(sync_session):
    """One ``manager_alert.created`` dispatch per alert, with the
    campaign_id/campaign_name lifted from evidence when present (campaign
    monitor) and absent otherwise (anomaly/trend alert)."""
    from backend.app.models import ManagerAlert
    from backend.app.services.manager_alert_fanout import fanout

    tenant, _, _, _ = _seed_tenant_with_recipients(sync_session)
    plain_alert = ManagerAlert(
        tenant_id=tenant.id,
        kind="topic_spike",
        severity="high",
        title="Refund mentions jumped 6x in 48 hours.",
        body="Baseline was 1/day; recent 12 calls.",
        evidence={"topic": "refund_request"},
        fingerprint="fp-plain",
    )
    campaign_id = uuid.uuid4()
    campaign_alert = ManagerAlert(
        tenant_id=tenant.id,
        kind="campaign_completed_summary",
        severity="low",
        title="Sweep finished.",
        body="No members left.",
        evidence={"campaign_id": str(campaign_id), "campaign_name": "July gyms sweep"},
        fingerprint="fp-campaign",
    )
    sync_session.add_all([plain_alert, campaign_alert])
    sync_session.commit()

    calls: List[Dict[str, Any]] = []

    def _stub_dispatch_sync(db, tenant_id, event, payload, **kwargs):
        calls.append({"tenant_id": tenant_id, "event": event, "payload": payload})
        return []

    with patch(
        "backend.app.services.manager_alert_fanout.dispatch_sync",
        side_effect=_stub_dispatch_sync,
    ):
        fanout(sync_session, [plain_alert, campaign_alert])

    assert len(calls) == 2
    assert all(c["event"] == "manager_alert.created" for c in calls)
    assert all(c["tenant_id"] == tenant.id for c in calls)

    by_alert_id = {c["payload"]["alert_id"]: c["payload"] for c in calls}
    plain_payload = by_alert_id[str(plain_alert.id)]
    assert plain_payload["kind"] == "topic_spike"
    assert plain_payload["severity"] == "high"
    assert plain_payload["title"] == plain_alert.title
    assert plain_payload["body"] == plain_alert.body
    assert plain_payload["opened_at"] is not None
    assert plain_payload["campaign_id"] is None
    assert plain_payload["campaign_name"] is None

    campaign_payload = by_alert_id[str(campaign_alert.id)]
    assert campaign_payload["campaign_id"] == str(campaign_id)
    assert campaign_payload["campaign_name"] == "July gyms sweep"


def test_fanout_survives_dispatch_sync_exception(sync_session):
    """A webhook enqueue failure never blocks the in-app notification."""
    from backend.app.models import ManagerAlert, Notification
    from backend.app.services.manager_alert_fanout import fanout

    tenant, manager, admin, _ = _seed_tenant_with_recipients(sync_session)
    alert = ManagerAlert(
        tenant_id=tenant.id,
        kind="topic_spike",
        severity="medium",
        title="Some spike.",
        evidence={},
        fingerprint="fp-broken-webhook",
    )
    sync_session.add(alert)
    sync_session.commit()

    with patch(
        "backend.app.services.manager_alert_fanout.dispatch_sync",
        side_effect=RuntimeError("webhook table missing"),
    ):
        fanout(sync_session, [alert])  # must not raise
    sync_session.commit()

    notifs = sync_session.query(Notification).all()
    by_user = {str(n.user_id) for n in notifs}
    assert str(manager.id) in by_user
    assert str(admin.id) in by_user
