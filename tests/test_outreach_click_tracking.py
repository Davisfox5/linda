"""Outreach click tracking: HTML link rewrite + the public /t/{token}
redirect endpoint.

Rendering tests are pure functions. Endpoint tests mount only the
outreach_links router over the SQLite fixtures (same pattern as
test_outreach_api); RLS GUC binding is dialect-guarded so the SQLite
path skips it. The send-path integration (scheduler rewrites at send
time, persists tokens per recipient) lives in test_outreach_engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.services.outreach.common import (
    parse_config,
    render_email_html,
    strip_markers,
)

TEMPLATE = {
    "subject": "Quick question about {business_name}",
    "body": "Hi — saw you run {business_name}.",
    "sender_name": "Davis Fox",
    "sender_business": "Flex",
    "physical_address": "123 Main St, Nashville, TN 37201",
}


def _template(**overrides):
    cfg = parse_config({"template": {**TEMPLATE, **overrides}})
    return cfg.template


# ── Config flag ─────────────────────────────────────────────────────────


def test_track_clicks_defaults_false_and_echoes_back():
    assert parse_config({"template": TEMPLATE}).track_clicks is False
    cfg = parse_config({"template": TEMPLATE, "track_clicks": True})
    assert cfg.track_clicks is True
    # Echoed in the validated config dict the campaign API returns,
    # exactly like footer_text.
    assert cfg.model_dump(mode="json")["track_clicks"] is True


# ── HTML rewrite (unit) ─────────────────────────────────────────────────


def test_rewriter_swaps_href_keeps_display_text():
    seen = []

    def rewriter(url):
        seen.append(url)
        return "https://lindaai.net/t/TOK"

    html = render_email_html(
        "Check [**our** pricing](https://gym.example/p?a=1&b=2) today.",
        _template(),
        link_rewriter=rewriter,
    )
    # The rewriter received the REAL destination (unescaped)…
    assert seen == ["https://gym.example/p?a=1&b=2"]
    # …the href is the redirect, and the display text (markers included)
    # is untouched.
    assert '<a href="https://lindaai.net/t/TOK" style="color:#2563eb;">' in html
    assert "<b>our</b> pricing</a>" in html
    assert "gym.example" not in html


def test_rewriter_sees_normalized_scheme_for_bare_domains():
    # A scheme-less marker is normalized to https:// BEFORE the rewriter
    # runs, so the stored original_url (the redirect's 302 target) is
    # always an absolute URL.
    seen = []

    def rewriter(url):
        seen.append(url)
        return None

    render_email_html(
        "[Book a demo](flexonline.net/book)", _template(), link_rewriter=rewriter
    )
    assert seen == ["https://flexonline.net/book"]


def test_rewriter_none_result_keeps_original():
    html = render_email_html(
        "See [docs](https://gym.example/docs).",
        _template(),
        link_rewriter=lambda url: None,
    )
    assert '<a href="https://gym.example/docs"' in html


def test_no_rewriter_is_unchanged_rendering():
    body = "See [docs](https://gym.example/docs)."
    assert render_email_html(body, _template()) == render_email_html(
        body, _template(), link_rewriter=None
    )


def test_footer_and_mailto_never_rewritten():
    calls = []

    def rewriter(url):
        calls.append(url)
        return "https://lindaai.net/t/TOK"

    template = _template(
        footer_text="Flex · 123 Main St\nManage preferences: https://gym.example/prefs"
    )
    html = render_email_html(
        "Write [me](mailto:davis@flex.example) or see"
        " [docs](https://gym.example/docs).",
        template,
        link_rewriter=rewriter,
    )
    # Only the body's http(s) link went through the rewriter — the
    # mailto marker isn't a link at all, and the footer URL stays put.
    assert calls == ["https://gym.example/docs"]
    assert "mailto:davis@flex.example" in html
    assert "https://gym.example/prefs" in html


def test_plain_text_part_never_sees_the_rewrite():
    body = "See [docs](https://gym.example/docs)."
    assert strip_markers(body) == "See docs: https://gym.example/docs."


# ── Public /t/{token} endpoint ──────────────────────────────────────────


@pytest_asyncio.fixture
async def seeded(test_session_factory, test_tenant):
    """Campaign + member + recipient (sent an hour ago) + one link row,
    plus an outreach.* wildcard webhook subscription (the Flex shape)."""
    from backend.app.models import (
        Campaign,
        CampaignRecipient,
        Contact,
        Customer,
        OutreachLink,
        OutreachMember,
        Webhook,
    )

    async with test_session_factory() as s:
        campaign = Campaign(
            tenant_id=test_tenant.id,
            name="Sweep",
            channel="email",
            kind="outreach",
            status="active",
            config={},
        )
        s.add(campaign)
        await s.flush()
        customer = Customer(
            tenant_id=test_tenant.id, name="Iron Gym", pipeline_status="contacted"
        )
        s.add(customer)
        await s.flush()
        contact = Contact(
            tenant_id=test_tenant.id, customer_id=customer.id,
            email="owner@irongym.example", name="Owner",
        )
        s.add(contact)
        await s.flush()
        member = OutreachMember(
            tenant_id=test_tenant.id,
            campaign_id=campaign.id,
            customer_id=customer.id,
            contact_id=contact.id,
            state="in_sequence",
        )
        s.add(member)
        await s.flush()
        recipient = CampaignRecipient(
            campaign_id=campaign.id,
            tenant_id=test_tenant.id,
            contact_id=contact.id,
            customer_id=customer.id,
            email_address=contact.email,
            sent_at=datetime.now(timezone.utc) - timedelta(hours=1),
            step=0,
        )
        s.add(recipient)
        await s.flush()
        link = OutreachLink(
            token="tok-" + uuid.uuid4().hex,
            tenant_id=test_tenant.id,
            campaign_id=campaign.id,
            member_id=member.id,
            recipient_id=recipient.id,
            original_url="https://gym.example/p?a=1&b=2",
        )
        s.add(link)
        s.add(
            Webhook(
                tenant_id=test_tenant.id,
                url="https://flex.example/hooks/linda",
                events=["outreach.*"],
                secret="whsec",
                active=True,
            )
        )
        await s.commit()
        return {
            "campaign": campaign,
            "customer": customer,
            "member": member,
            "recipient": recipient,
            "link": link,
        }


@pytest_asyncio.fixture
async def client(test_session_factory):
    from fastapi import FastAPI

    from backend.app.api.outreach_links import router as links_router
    from backend.app.db import get_db

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(links_router)
    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_token_round_trip_302_and_click_recorded(
    client, seeded, test_session_factory, test_tenant
):
    from backend.app.models import CampaignEvent, WebhookDelivery

    link = seeded["link"]
    resp = await client.get(
        f"/t/{link.token}",
        headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://gym.example/p?a=1&b=2"

    async with test_session_factory() as s:
        event = (await s.execute(select(CampaignEvent))).scalars().one()
        assert event.event_type == "click"
        assert event.tenant_id == test_tenant.id
        assert event.campaign_id == seeded["campaign"].id
        assert event.recipient_id == seeded["recipient"].id
        assert event.contact_id == seeded["recipient"].contact_id
        assert event.metadata_["url"] == "https://gym.example/p?a=1&b=2"
        assert event.metadata_["suspected_bot"] is False
        assert event.metadata_["ip_hash"]  # hashed, never the raw IP
        assert "testclient" not in str(event.metadata_.get("ip_hash"))

        delivery = (await s.execute(select(WebhookDelivery))).scalars().one()
        assert delivery.event == "outreach.link_clicked"
        data = delivery.payload["data"]
        assert data["url"] == "https://gym.example/p?a=1&b=2"
        assert data["prospect_id"] == str(seeded["customer"].id)
        assert data["campaign_name"] == "Sweep"
        assert data["suspected_bot"] is False


@pytest.mark.asyncio
async def test_unknown_token_falls_back_safely(client, seeded, test_session_factory):
    from backend.app.models import CampaignEvent

    resp = await client.get("/t/definitely-not-a-token")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://lindaai.net"
    async with test_session_factory() as s:
        assert (await s.execute(select(CampaignEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_every_hit_recorded_and_scanner_hits_flagged(
    client, seeded, test_session_factory
):
    from backend.app.models import CampaignEvent, CampaignRecipient

    link = seeded["link"]
    # Scanner-shaped hit right after delivery…
    async with test_session_factory() as s:
        r = await s.get(CampaignRecipient, seeded["recipient"].id)
        r.sent_at = datetime.now(timezone.utc)
        await s.commit()
    resp = await client.get(
        f"/t/{link.token}", headers={"User-Agent": "Barracuda Sentinel scanner"}
    )
    assert resp.status_code == 302
    # …then two human clicks on the same link.
    for _ in range(2):
        async with test_session_factory() as s:
            r = await s.get(CampaignRecipient, seeded["recipient"].id)
            r.sent_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await s.commit()
        resp = await client.get(
            f"/t/{link.token}", headers={"User-Agent": "Mozilla/5.0"}
        )
        assert resp.status_code == 302

    async with test_session_factory() as s:
        events = (await s.execute(select(CampaignEvent))).scalars().all()
    # Every hit lands as its own row — flagged, never dropped.
    assert len(events) == 3
    flags = sorted(e.metadata_["suspected_bot"] for e in events)
    assert flags == [False, False, True]
    flagged = [e for e in events if e.metadata_["suspected_bot"]][0]
    assert "bot_user_agent" in flagged.metadata_["bot_signals"]
