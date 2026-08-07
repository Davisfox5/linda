"""Engine tests: the outreach scheduler tick + ingest-side hooks.

Uses a *sync* SQLite session (the engine runs inside Celery on sync
sessions). The provider sender is faked at the transport seam
(scheduler.build_sender) so no HTTP/OAuth is involved; Celery enqueues
inside dispatch_sync fail quietly with no broker, which is exactly the
production no-webhook-configured posture.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import tests.db_fixtures  # noqa: F401 — registers JSONB/UUID sqlite compilers

from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    Contact,
    Customer,
    EmailSend,
    Integration,
    Interaction,
    OutreachMember,
    Tenant,
)
from backend.app.services.outreach import replies, scheduler
from backend.app.services.outreach.common import parse_config

# Wednesday 15:00 UTC == 11:00 New York — inside the default send window.
IN_WINDOW = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
# Sunday — outside.
OUT_OF_WINDOW = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)

CONFIG = {
    "template": {
        "subject": "Quick question about {business_name}",
        "body": "Hi — saw you run {business_name}.",
        "sender_name": "Davis Fox",
        "sender_business": "Flex",
        "physical_address": "123 Main St, Nashville, TN 37201",
    },
    "daily_limit": 2,
    "max_touches": 2,
    "steps": [{"offset_days": 0}, {"offset_days": 3}],
    "mode": "review",
}


@dataclass
class FakeSendResult:
    provider: str = "google"
    message_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    raw_snippet: str = ""


class FakeSender:
    """Stands in for GmailSender at the scheduler's build_sender seam."""

    calls: List[dict] = []
    fail = False

    def __init__(self) -> None:
        self.provider = "google"

    async def send(self, **kwargs):
        if FakeSender.fail:
            raise RuntimeError("provider exploded")
        FakeSender.calls.append(kwargs)
        n = len(FakeSender.calls)
        return FakeSendResult(
            message_id=f"<out-{n}@mail.example>", provider_message_id=f"gm-{n}"
        )

    async def close(self) -> None:
        pass


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://")
    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def fake_sender(monkeypatch):
    FakeSender.calls = []
    FakeSender.fail = False
    monkeypatch.setattr(scheduler, "build_sender", lambda integ, from_address_hint=None: FakeSender())
    return FakeSender


@pytest.fixture(autouse=True)
def copy_gate_off(monkeypatch):
    """The stub drafts these tests seed ("Body 0") would never clear the
    house copy gate — keep it off for the send-machinery tests. The
    gate's own tests (bottom of this file) flip it back on."""
    from backend.app.config import get_settings

    # get_settings() is cached; flip the flag on the live instance (same
    # pattern as test_action_plan_executor).
    monkeypatch.setattr(get_settings(), "OUTREACH_COPY_GATE_ENABLED", False)


def _seed(db: Session, n_members: int = 1, config: Optional[dict] = None):
    tenant = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    db.flush()
    db.add(
        Integration(
            tenant_id=tenant.id, provider="google",
            access_token="enc", refresh_token="enc",
        )
    )
    campaign = Campaign(
        tenant_id=tenant.id,
        name="Sweep",
        channel="email",
        kind="outreach",
        status="active",
        config=parse_config(config or CONFIG).model_dump(mode="json"),
    )
    db.add(campaign)
    db.flush()
    members = []
    for i in range(n_members):
        customer = Customer(
            tenant_id=tenant.id,
            name=f"Gym {i}",
            domain=f"gym-{i}.com",
            pipeline_status="new",
            metadata_={"outreach": {"hook": f"Hook {i}", "segment": "boutique"}},
        )
        db.add(customer)
        db.flush()
        contact = Contact(
            tenant_id=tenant.id, customer_id=customer.id,
            email=f"owner{i}@gym-{i}.com", name=f"Owner {i}",
        )
        db.add(contact)
        db.flush()
        member = OutreachMember(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            customer_id=customer.id,
            contact_id=contact.id,
            state="queued",
            draft_status="approved",
            draft_subject=f"Subject {i}",
            draft_body=f"Body {i}",
            next_send_at=IN_WINDOW - timedelta(minutes=5),
        )
        db.add(member)
        members.append(member)
    db.commit()
    return tenant, campaign, members


# ── Sending ────────────────────────────────────────────────────────────


def test_tick_sends_due_member_and_writes_all_rows(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 1 and result["failed"] == 0

    sent_kwargs = fake_sender.calls[0]
    assert sent_kwargs["to"] == ["owner0@gym-0.com"]
    # CAN-SPAM footer appended to the approved draft body.
    assert "Body 0" in sent_kwargs["body"]
    assert "123 Main St" in sent_kwargs["body"]
    assert "unsubscribe" in sent_kwargs["body"].lower()

    send = db.execute(select(EmailSend)).scalars().one()
    assert send.status == "sent"
    assert send.campaign_id == campaign.id
    assert send.customer_id == member.customer_id
    assert send.provider_message_id == "gm-1"

    recipient = db.execute(select(CampaignRecipient)).scalars().one()
    assert recipient.rfc822_message_id == "<out-1@mail.example>"
    assert recipient.customer_id == member.customer_id
    assert recipient.step == 0

    interaction = db.execute(select(Interaction)).scalars().one()
    assert interaction.direction == "outbound"
    assert interaction.campaign_id == campaign.id
    assert interaction.customer_id == member.customer_id
    assert interaction.provider_message_id == "gm-1"

    db.refresh(member)
    assert member.state == "in_sequence"
    assert member.touches_sent == 1
    assert member.current_step == 1
    assert member.thread_message_ids == ["<out-1@mail.example>"]
    assert member.next_send_at is not None  # bump scheduled

    customer = db.get(Customer, member.customer_id)
    assert customer.pipeline_status == "contacted"


def test_tick_respects_daily_limit_and_window(db, fake_sender):
    tenant, campaign, members = _seed(db, n_members=3)

    # daily_limit=2 → only 2 of 3 send this "day".
    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 2
    assert len(fake_sender.calls) == 2

    again = scheduler.run_campaign_tick(
        db, tenant, campaign, now_utc=IN_WINDOW + timedelta(minutes=10)
    )
    assert again["sent"] == 0  # quota exhausted

    # Outside the window nothing sends, quota or not.
    fake_sender.calls.clear()
    outside = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=OUT_OF_WINDOW)
    assert outside["sent"] == 0
    assert fake_sender.calls == []


def test_tick_skips_dnc_and_survives_provider_failure(db, fake_sender):
    tenant, campaign, members = _seed(db, n_members=2)
    # First member goes DNC between approval and send.
    customer0 = db.get(Customer, members[0].customer_id)
    customer0.do_not_contact = True
    db.commit()

    FakeSender.fail = True
    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 0
    # one halted (DNC, not a failure), one failed on the provider
    db.refresh(members[0])
    db.refresh(members[1])
    assert members[0].state == "halted" and members[0].halt_reason == "do_not_contact"
    assert members[1].state == "failed"
    failed_send = db.execute(select(EmailSend)).scalars().one()
    assert failed_send.status == "failed"
    assert "provider exploded" in failed_send.error


def test_sequence_completes_after_max_touches(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    db.refresh(member)

    # Fast-forward past the bump offset; regenerate inline via a stub.
    member.state = "queued"
    member.draft_status = "approved"
    member.draft_subject = "Bump"
    member.draft_body = "Bump body"
    member.next_send_at = IN_WINDOW
    db.commit()
    day2 = IN_WINDOW + timedelta(days=1)  # fresh throttle day
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=day2)

    db.refresh(member)
    assert member.touches_sent == 2
    assert member.state == "completed"  # max_touches=2 exhausted
    assert member.next_send_at is None
    # Second send threads on the first.
    threading = fake_sender.calls[1]
    assert threading["in_reply_to"] == "<out-1@mail.example>"
    assert threading["references"] == ["<out-1@mail.example>"]

    db.refresh(campaign)
    assert campaign.status == "completed"  # no actionable members left
    assert campaign.sent_count == 2


def test_bump_reenters_draft_flow(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    db.refresh(member)
    assert member.state == "in_sequence"

    # Stub the LLM personalization for the bump draft.
    monkeypatch.setattr(
        scheduler.drafts_mod,
        "generate_member_draft",
        lambda campaign, config, member, customer, step_index=None: {
            "subject": "Bump subject", "body": "Bump body", "facts": {}
        },
    )
    after_bump_due = member.next_send_at + timedelta(hours=1)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=after_bump_due)
    db.refresh(member)
    # review mode → bump waits for human approval, not auto-sent
    assert member.state == "needs_approval"
    assert member.draft_subject == "Bump subject"
    assert member.draft_status == "ready"


# ── Reply / bounce / opt-out hooks ─────────────────────────────────────


def _reply_interaction(db, tenant, member, text: str) -> Interaction:
    contact = db.get(Contact, member.contact_id)
    interaction = Interaction(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel="email",
        direction="inbound",
        from_address=contact.email,
        subject="Re: Quick question",
        raw_text=text,
        message_id=f"<reply-{uuid.uuid4().hex[:6]}@gym>",
        in_reply_to="<out-1@mail.example>",
    )
    db.add(interaction)
    db.flush()
    return interaction


def test_reply_halts_sequence_and_flips_status(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    recipient = replies.find_recipient_for_reply(
        db, tenant.id,
        in_reply_to="<out-1@mail.example>", references=None, from_address=None,
    )
    assert recipient is not None

    interaction = _reply_interaction(db, tenant, member, "Sounds interesting — tell me more!")
    replies.handle_outreach_reply(db, tenant.id, interaction, recipient, db.get(Contact, member.contact_id))
    db.flush()

    db.refresh(member)
    assert member.state == "replied"
    assert member.next_send_at is None
    customer = db.get(Customer, member.customer_id)
    assert customer.pipeline_status == "replied"
    assert interaction.customer_id == customer.id  # hung onto the prospect


def test_reply_matches_via_references_chain_and_address_fallback(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    by_refs = replies.find_recipient_for_reply(
        db, tenant.id,
        in_reply_to=None,
        references=["<out-1@mail.example>", "<other@x>"],
        from_address=None,
    )
    assert by_refs is not None

    # Outlook path: no Message-ID we know — match by sender address while
    # the member is awaiting a reply.
    by_addr = replies.find_recipient_for_reply(
        db, tenant.id,
        in_reply_to=None, references=None, from_address="owner0@gym-0.com",
    )
    assert by_addr is not None

    assert replies.find_recipient_for_reply(
        db, tenant.id, in_reply_to=None, references=None,
        from_address="stranger@nowhere.com",
    ) is None


def test_opt_out_reply_sets_dnc_and_halts_everything(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    # A second campaign enrollment for the same prospect must halt too.
    other = Campaign(
        tenant_id=tenant.id, name="Other", channel="email",
        kind="outreach", status="active",
        config=parse_config(CONFIG).model_dump(mode="json"),
    )
    db.add(other)
    db.flush()
    sibling = OutreachMember(
        tenant_id=tenant.id, campaign_id=other.id,
        customer_id=member.customer_id, contact_id=member.contact_id,
        state="queued", draft_status="approved",
    )
    db.add(sibling)
    db.commit()

    recipient = replies.find_recipient_for_reply(
        db, tenant.id, in_reply_to="<out-1@mail.example>",
        references=None, from_address=None,
    )
    interaction = _reply_interaction(db, tenant, member, "Please remove me from your list.")
    replies.handle_outreach_reply(
        db, tenant.id, interaction, recipient, db.get(Contact, member.contact_id)
    )
    db.flush()

    db.refresh(member)
    db.refresh(sibling)
    assert member.state == "opted_out"
    assert sibling.state == "halted"
    customer = db.get(Customer, member.customer_id)
    assert customer.do_not_contact is True
    assert customer.pipeline_status == "do_not_contact"
    events = db.execute(select(CampaignEvent)).scalars().all()
    assert "unsubscribe" in {e.event_type for e in events}


def test_bounce_detection_marks_member(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)

    handled = replies.handle_possible_bounce(
        db, tenant.id,
        from_address="MAILER-DAEMON@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body_text="The message <out-1@mail.example> to owner0@gym-0.com bounced.",
        in_reply_to=None,
        references=None,
    )
    assert handled is True
    db.refresh(member)
    assert member.state == "bounced"
    events = db.execute(select(CampaignEvent)).scalars().all()
    assert "bounce" in {e.event_type for e in events}

    # A random newsletter that merely *looks* auto-generated is ignored.
    assert not replies.handle_possible_bounce(
        db, tenant.id,
        from_address="mailer-daemon@elsewhere.com",
        subject="Undeliverable",
        body_text="no known ids here",
        in_reply_to=None,
        references=None,
    )


def test_reply_never_demotes_advanced_prospect(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    customer = db.get(Customer, member.customer_id)
    customer.pipeline_status = "demo"  # rep already booked the demo
    db.commit()

    recipient = replies.find_recipient_for_reply(
        db, tenant.id, in_reply_to="<out-1@mail.example>",
        references=None, from_address=None,
    )
    interaction = _reply_interaction(db, tenant, member, "See you at the demo")
    replies.handle_outreach_reply(
        db, tenant.id, interaction, recipient, db.get(Contact, member.contact_id)
    )
    db.refresh(customer)
    assert customer.pipeline_status == "demo"  # monotonic — not demoted


# ── HTML rendering / logo / attachments in the send path ───────────────


class FakeStore:
    """Stands in for the S3 attachment store at the scheduler seam."""

    def __init__(self, objects=None):
        self.objects = objects or {}

    def get(self, key):
        return self.objects.get(key)


def test_send_passes_html_alternative_and_strips_markers(db, fake_sender):
    tenant, campaign, (member,) = _seed(db)
    member.draft_body = "Hi **Sam**, try *this* and _that_."
    db.commit()

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    kw = fake_sender.calls[0]
    # Plain part: markers stripped, footer appended.
    assert "Hi Sam, try this and that." in kw["body"]
    assert "**" not in kw["body"]
    # HTML part: markers rendered, footer + no logo (none uploaded).
    assert "<b>Sam</b>" in kw["body_html"]
    assert "<i>this</i>" in kw["body_html"]
    assert "<u>that</u>" in kw["body_html"]
    assert "123 Main St" in kw["body_html"]
    assert "cid:" not in kw["body_html"]
    assert kw["attachments"] is None

    interaction = db.execute(select(Interaction)).scalars().one()
    assert "<b>Sam</b>" in interaction.body_html


def test_send_embeds_tenant_logo_inline(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    logo_key = f"tenants/{tenant.id}/branding/ab12-logo.png"
    tenant.branding_config = {
        "email_logo": {
            "s3_key": logo_key,
            "filename": "logo.png",
            "content_type": "image/png",
        }
    }
    db.commit()
    store = FakeStore({logo_key: (b"\x89PNGDATA", "image/png")})
    monkeypatch.setattr(scheduler, "get_store", lambda: store)

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    kw = fake_sender.calls[0]
    (logo,) = kw["attachments"]
    assert logo.content_id == "tenant-logo"
    assert logo.data == b"\x89PNGDATA"
    assert logo.content_type == "image/png"
    assert 'src="cid:tenant-logo"' in kw["body_html"]


def test_send_attaches_campaign_files(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    key = f"tenants/{tenant.id}/outreach/ab12-deck.pdf"
    campaign.config = {
        **campaign.config,
        "attachments": [
            {"s3_key": key, "filename": "deck.pdf", "content_type": "application/pdf"}
        ],
    }
    db.commit()
    store = FakeStore({key: (b"%PDF", "application/pdf")})
    monkeypatch.setattr(scheduler, "get_store", lambda: store)

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    (att,) = fake_sender.calls[0]["attachments"]
    assert att.filename == "deck.pdf" and att.data == b"%PDF"
    assert att.content_id is None

    send = db.execute(select(EmailSend)).scalars().one()
    assert send.attachments[0]["filename"] == "deck.pdf"


def test_send_fails_member_when_attachment_unavailable(db, fake_sender, monkeypatch):
    tenant, campaign, (member,) = _seed(db)
    campaign.config = {
        **campaign.config,
        "attachments": [
            # Wrong tenant prefix — must never be fetched or sent.
            {"s3_key": "tenants/other/outreach/x-deck.pdf", "filename": "deck.pdf"}
        ],
    }
    db.commit()
    monkeypatch.setattr(scheduler, "get_store", lambda: FakeStore())

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert fake_sender.calls == []
    db.refresh(member)
    assert member.state == "failed"
    assert member.halt_reason == "attachment_unavailable"


# ── Click tracking (out_002) ───────────────────────────────────────────


def test_track_clicks_rewrites_html_part_only_and_persists_links(
    db, fake_sender, monkeypatch
):
    from backend.app.models import OutreachLink
    from backend.app.services.outreach import links as links_mod

    monkeypatch.setattr(
        links_mod, "tracking_base_url", lambda: "https://lindaai.net"
    )
    tenant, campaign, (member,) = _seed(
        db, config={**CONFIG, "track_clicks": True}
    )
    member.draft_body = (
        "See [**our** pricing](https://gym.example/p?a=1&b=2) and "
        "[docs](https://gym.example/docs).\n\n"
        "Same link again: [pricing](https://gym.example/p?a=1&b=2)."
    )
    db.commit()

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    kw = fake_sender.calls[0]

    # Plain part keeps the ORIGINAL urls — never the redirect.
    assert "https://gym.example/p?a=1&b=2" in kw["body"]
    assert "/t/" not in kw["body"]
    # HTML hrefs point at the redirect; display text/markers unchanged.
    assert 'href="https://gym.example' not in kw["body_html"]
    assert "<b>our</b> pricing</a>" in kw["body_html"]

    rows = db.execute(select(OutreachLink)).scalars().all()
    # One row per DISTINCT destination — the repeated pricing link
    # reuses its token, so both its anchors share one row.
    assert sorted(r.original_url for r in rows) == [
        "https://gym.example/docs",
        "https://gym.example/p?a=1&b=2",
    ]
    recipient = db.execute(select(CampaignRecipient)).scalars().one()
    for r in rows:
        assert r.tenant_id == tenant.id
        assert r.campaign_id == campaign.id
        assert r.member_id == member.id
        assert r.recipient_id == recipient.id
        assert f'href="https://lindaai.net/t/{r.token}"' in kw["body_html"]
    # 3 anchors total: the two pricing anchors share one token + docs.
    assert kw["body_html"].count("https://lindaai.net/t/") == 3


def test_track_clicks_off_by_default_sends_original_links(db, fake_sender):
    from backend.app.models import OutreachLink

    tenant, campaign, (member,) = _seed(db)  # CONFIG has no track_clicks
    member.draft_body = "See [pricing](https://gym.example/p)."
    db.commit()

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    kw = fake_sender.calls[0]
    assert 'href="https://gym.example/p"' in kw["body_html"]
    assert "/t/" not in kw["body_html"]
    assert db.execute(select(OutreachLink)).scalars().all() == []


def test_track_clicks_without_base_url_declines_rewrite(db, fake_sender):
    from backend.app.models import OutreachLink

    # Neither OUTREACH_TRACKING_BASE_URL nor PUBLIC_WEBHOOK_BASE_URL is
    # set in tests → the rewriter declines rather than emit dead links.
    tenant, campaign, (member,) = _seed(
        db, config={**CONFIG, "track_clicks": True}
    )
    member.draft_body = "See [pricing](https://gym.example/p)."
    db.commit()

    scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    kw = fake_sender.calls[0]
    assert 'href="https://gym.example/p"' in kw["body_html"]
    assert db.execute(select(OutreachLink)).scalars().all() == []


# ── Copy gate (send-time backstop + auto-mode approval) ────────────────


@pytest.fixture
def copy_gate_on(monkeypatch):
    from backend.app.config import get_settings

    monkeypatch.setattr(get_settings(), "OUTREACH_COPY_GATE_ENABLED", True)


def test_send_time_gate_parks_failing_approved_draft(db, fake_sender, copy_gate_on):
    """Even a human-approved draft that fails the gate must not send —
    it flips back to needs_approval with the draft kept, so it surfaces
    in the review inbox instead of going out."""
    tenant, campaign, (member,) = _seed(db)
    failing = "Quick note — worth a look?"  # em dash, way under 50 words
    member.draft_body = failing
    db.commit()

    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 0
    assert fake_sender.calls == []
    db.refresh(member)
    assert member.state == "needs_approval"
    assert member.draft_status == "ready"
    assert member.draft_body == failing  # draft kept for the reviewer
    assert member.next_send_at is None
    failures = member.personalization["copy_gate_failures"]
    assert any("em/en dashes" in f for f in failures)
    assert any("word count" in f for f in failures)
    assert db.execute(select(EmailSend)).scalars().all() == []


def test_send_time_gate_passes_clean_draft_and_clears_stale_failures(
    db, fake_sender, copy_gate_on
):
    from tests.test_outreach_copy_gate import send_ready_draft

    tenant, campaign, (member,) = _seed(db)
    member.draft_body = send_ready_draft("Gym 0")  # normalized to "Acme"
    member.personalization = {"copy_gate_failures": ["stale from a prior draft"]}
    db.commit()

    result = scheduler.run_campaign_tick(db, tenant, campaign, now_utc=IN_WINDOW)
    assert result["sent"] == 1
    db.refresh(member)
    assert member.state == "in_sequence"
    assert "copy_gate_failures" not in (member.personalization or {})


def _reset_for_generation(db, member):
    member.state = "draft_pending"
    member.draft_status = None
    member.draft_subject = None
    member.draft_body = None
    member.next_send_at = None
    db.commit()


def test_auto_mode_gate_parks_failing_draft_as_needs_approval(
    db, fake_sender, copy_gate_on, monkeypatch
):
    tenant, campaign, (member,) = _seed(db, config={**CONFIG, "mode": "auto"})
    _reset_for_generation(db, member)
    monkeypatch.setattr(
        scheduler.drafts_mod,
        "generate_member_draft",
        lambda campaign, config, member, customer, step_index=None: {
            "subject": "Hello",
            "body": "Saw your gym — impressive!",  # dropped subject + em dash
            "facts": {"hook": "Hook 0"},
        },
    )
    out = scheduler.generate_drafts_for_campaign(db, tenant, campaign)
    assert out["generated"] == 1
    db.refresh(member)
    # Parked for review, NOT self-approved.
    assert member.state == "needs_approval"
    assert member.draft_status == "ready"
    assert member.next_send_at is None
    failures = member.personalization["copy_gate_failures"]
    assert any("dropped-subject" in f for f in failures)
    assert member.personalization["hook"] == "Hook 0"  # facts kept alongside


def test_auto_mode_gate_passing_draft_still_self_approves(
    db, fake_sender, copy_gate_on, monkeypatch
):
    from tests.test_outreach_copy_gate import send_ready_draft

    tenant, campaign, (member,) = _seed(db, config={**CONFIG, "mode": "auto"})
    _reset_for_generation(db, member)
    monkeypatch.setattr(
        scheduler.drafts_mod,
        "generate_member_draft",
        lambda campaign, config, member, customer, step_index=None: {
            "subject": "Hello",
            "body": send_ready_draft("Gym 0"),
            "facts": {},
        },
    )
    scheduler.generate_drafts_for_campaign(db, tenant, campaign)
    db.refresh(member)
    assert member.state == "queued"
    assert member.draft_status == "approved"
    assert "copy_gate_failures" not in (member.personalization or {})


def test_gate_disabled_keeps_old_auto_approve_behavior(db, fake_sender, monkeypatch):
    # copy_gate_off (autouse) is in effect — the emergency switch restores
    # the pre-gate behavior end to end.
    tenant, campaign, (member,) = _seed(db, config={**CONFIG, "mode": "auto"})
    _reset_for_generation(db, member)
    monkeypatch.setattr(
        scheduler.drafts_mod,
        "generate_member_draft",
        lambda campaign, config, member, customer, step_index=None: {
            "subject": "Hello", "body": "Too short — fails the gate.", "facts": {}
        },
    )
    scheduler.generate_drafts_for_campaign(db, tenant, campaign)
    db.refresh(member)
    assert member.state == "queued"
    assert member.draft_status == "approved"
