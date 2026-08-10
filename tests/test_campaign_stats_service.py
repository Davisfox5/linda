"""Tests for backend/app/services/campaign_stats.py.

This module was extracted from api/campaigns.py's ``_compute_rollup`` and
api/outreach.py's ``_member_states``/``_quota_state`` (Phase 1 of
docs/plans/campaign-monitoring.md) so both the REST routers and the Ask
LINDA chat tools share one set of campaign metric definitions. These tests
guard the extraction: the bot-click filtering, funnel, quota, and
both-kinds overview behavior must survive the move verbatim.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio

VALID_OUTREACH_CONFIG = {
    "template": {
        "subject": "Quick question about {business_name}",
        "body": "Hi — saw you run {business_name}.",
        "sender_name": "Davis Fox",
        "sender_business": "Flex",
        "physical_address": "123 Main St, Nashville, TN 37201",
    },
    "daily_limit": 10,
    "max_touches": 3,
    "mode": "review",
}


async def _make_campaign(session, tenant, **overrides):
    from backend.app.models import Campaign

    defaults = dict(
        tenant_id=tenant.id,
        name="Spring blast",
        channel="email",
        kind="external",
        status="active",
    )
    defaults.update(overrides)
    campaign = Campaign(**defaults)
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


async def _make_recipient(session, tenant, campaign, **overrides):
    from backend.app.models import CampaignRecipient

    defaults = dict(
        campaign_id=campaign.id,
        tenant_id=tenant.id,
        email_address="prospect@example.com",
    )
    defaults.update(overrides)
    recipient = CampaignRecipient(**defaults)
    session.add(recipient)
    await session.commit()
    await session.refresh(recipient)
    return recipient


async def _make_event(session, tenant, campaign, event_type, recipient=None, metadata=None):
    from backend.app.models import CampaignEvent

    event = CampaignEvent(
        campaign_id=campaign.id,
        tenant_id=tenant.id,
        recipient_id=recipient.id if recipient else None,
        event_type=event_type,
        metadata_=metadata or {},
    )
    session.add(event)
    await session.commit()
    return event


# ── compute_rollup ──────────────────────────────────────────────────────


async def test_compute_rollup_counts_by_event_type(test_session_factory, test_tenant):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(session, test_tenant)
        r1 = await _make_recipient(session, test_tenant, campaign, email_address="a@x.com")
        await _make_recipient(session, test_tenant, campaign, email_address="b@x.com")
        await _make_event(session, test_tenant, campaign, "open", r1)
        await _make_event(session, test_tenant, campaign, "bounce", r1)
        await _make_event(session, test_tenant, campaign, "unsubscribe", r1)
        await _make_event(session, test_tenant, campaign, "convert", r1)

        rollup = await campaign_stats.compute_rollup(session, test_tenant, campaign.id)

    assert rollup["sent"] == 2
    assert rollup["opens"] == 1
    assert rollup["bounces"] == 1
    assert rollup["unsubscribes"] == 1
    assert rollup["conversions"] == 1
    assert rollup["replies"] == 0
    assert rollup["reply_sentiment_avg"] is None


async def test_compute_rollup_unique_clicks_filters_suspected_bots_and_dedupes(
    test_session_factory, test_tenant
):
    """Regression guard for the move: unique_clicks must collapse repeats to
    one per (recipient, url) and exclude suspected_bot hits, exactly as the
    router's ``_compute_rollup`` did before extraction."""
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(session, test_tenant)
        recipient = await _make_recipient(session, test_tenant, campaign)

        # Two human clicks on the same URL (dedupe to 1), one on a second URL
        # (counts separately), and one bot-flagged click (excluded).
        await _make_event(
            session, test_tenant, campaign, "click", recipient,
            metadata={"url": "https://a.com", "suspected_bot": False},
        )
        await _make_event(
            session, test_tenant, campaign, "click", recipient,
            metadata={"url": "https://a.com", "suspected_bot": False},
        )
        await _make_event(
            session, test_tenant, campaign, "click", recipient,
            metadata={"url": "https://b.com", "suspected_bot": False},
        )
        await _make_event(
            session, test_tenant, campaign, "click", recipient,
            metadata={"url": "https://a.com", "suspected_bot": True},
        )
        # No click-tracking metadata at all (external ESP) — absent
        # suspected_bot must still count as human.
        await _make_event(
            session, test_tenant, campaign, "click", recipient,
            metadata={"url": "https://c.com"},
        )

        rollup = await campaign_stats.compute_rollup(session, test_tenant, campaign.id)

    assert rollup["clicks"] == 5  # every recorded click event
    assert rollup["unique_clicks"] == 3  # a.com (x1 deduped), b.com, c.com


async def test_compute_rollup_averages_reply_sentiment_from_attributed_interactions(
    test_session_factory, test_tenant
):
    from backend.app.models import Interaction
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(session, test_tenant)

        session.add(Interaction(
            tenant_id=test_tenant.id,
            campaign_id=campaign.id,
            channel="email",
            direction="inbound",
            insights={"sentiment_score": 0.8},
        ))
        session.add(Interaction(
            tenant_id=test_tenant.id,
            campaign_id=campaign.id,
            channel="email",
            direction="inbound",
            insights={"sentiment_score": 0.4},
        ))
        # Outbound interaction on the same campaign must not count.
        session.add(Interaction(
            tenant_id=test_tenant.id,
            campaign_id=campaign.id,
            channel="email",
            direction="outbound",
            insights={"sentiment_score": -1.0},
        ))
        await session.commit()

        rollup = await campaign_stats.compute_rollup(session, test_tenant, campaign.id)

    assert rollup["reply_sentiment_avg"] == pytest.approx(0.6)


# ── member_states / quota_state ──────────────────────────────────────────


async def test_member_states_counts_by_state(test_session_factory, test_tenant):
    from backend.app.models import OutreachMember
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(
            session, test_tenant, kind="outreach", status="active", config=VALID_OUTREACH_CONFIG
        )
        for state in ("queued", "queued", "in_sequence", "replied"):
            session.add(OutreachMember(
                tenant_id=test_tenant.id,
                campaign_id=campaign.id,
                customer_id=uuid.uuid4(),
                state=state,
            ))
        await session.commit()

        states = await campaign_stats.member_states(session, campaign.id)

    assert states == {"queued": 2, "in_sequence": 1, "replied": 1}


async def test_quota_state_reports_daily_limit_and_counts(test_session_factory, test_tenant):
    from backend.app.models import EmailSend
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(
            session, test_tenant, kind="outreach", status="active", config=VALID_OUTREACH_CONFIG
        )
        # A sent email attributed to this campaign, today.
        session.add(EmailSend(
            tenant_id=test_tenant.id,
            provider="google",
            to_address="a@x.com",
            subject="hi",
            body="hi",
            status="sent",
            campaign_id=campaign.id,
        ))
        # A sent email attributed to a *different* campaign (still counts
        # toward the tenant-wide daily cap, not the campaign-scoped count).
        other_campaign = await _make_campaign(
            session, test_tenant, kind="outreach", status="active",
            name="Other", config=VALID_OUTREACH_CONFIG,
        )
        session.add(EmailSend(
            tenant_id=test_tenant.id,
            provider="google",
            to_address="b@x.com",
            subject="hi",
            body="hi",
            status="sent",
            campaign_id=other_campaign.id,
        ))
        await session.commit()

        quota = await campaign_stats.quota_state(session, test_tenant.id, campaign)

    assert quota is not None
    assert quota["daily_limit"] == 10
    assert quota["sent_today"] == 1
    assert quota["remaining_today"] == 9
    assert quota["tenant_sent_today"] == 2


async def test_quota_state_returns_none_for_unparseable_config(test_session_factory, test_tenant):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(
            session, test_tenant, kind="outreach", status="draft", config={}
        )
        quota = await campaign_stats.quota_state(session, test_tenant.id, campaign)

    assert quota is None


# ── list_campaigns ──────────────────────────────────────────────────────


async def test_list_campaigns_orders_by_started_at_then_created_at_fallback(
    test_session_factory, test_tenant
):
    from backend.app.services import campaign_stats

    now = datetime.now(timezone.utc)
    async with test_session_factory() as session:
        # No started_at — falls back to created_at, oldest of the three.
        no_start = await _make_campaign(
            session, test_tenant, name="No start yet",
            created_at=now - timedelta(days=10),
        )
        # started_at explicitly in the middle.
        mid = await _make_campaign(
            session, test_tenant, name="Mid",
            started_at=now - timedelta(days=5),
            created_at=now - timedelta(days=20),
        )
        # Most recent started_at.
        newest = await _make_campaign(
            session, test_tenant, name="Newest",
            started_at=now - timedelta(days=1),
            created_at=now - timedelta(days=30),
        )

        rows = await campaign_stats.list_campaigns(session, test_tenant)

    ids = [r["id"] for r in rows]
    assert ids == [str(newest.id), str(mid.id), str(no_start.id)]


async def test_list_campaigns_filters_by_kind_and_status(test_session_factory, test_tenant):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        await _make_campaign(session, test_tenant, name="Ext active", kind="external", status="active")
        await _make_campaign(session, test_tenant, name="Outreach draft", kind="outreach", status="draft", config=VALID_OUTREACH_CONFIG)

        outreach_only = await campaign_stats.list_campaigns(session, test_tenant, kind="outreach")
        active_only = await campaign_stats.list_campaigns(session, test_tenant, status="active")

    assert [r["name"] for r in outreach_only] == ["Outreach draft"]
    assert [r["name"] for r in active_only] == ["Ext active"]


async def test_list_campaigns_header_includes_kind_and_sent_count(test_session_factory, test_tenant):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(session, test_tenant, sent_count=42)
        rows = await campaign_stats.list_campaigns(session, test_tenant)

    assert rows[0]["kind"] == "external"
    assert rows[0]["status"] == "active"
    assert rows[0]["sent_count"] == 42


# ── campaign_overview: both-kinds shape ──────────────────────────────────


async def test_campaign_overview_external_has_no_member_states_or_quota(
    test_session_factory, test_tenant
):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(session, test_tenant, kind="external")
        overview = await campaign_stats.campaign_overview(session, test_tenant, campaign.id)

    assert overview["kind"] == "external"
    assert "rollup" in overview
    assert "member_states" not in overview
    assert "quota" not in overview


async def test_campaign_overview_outreach_includes_member_states_and_quota(
    test_session_factory, test_tenant
):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(
            session, test_tenant, kind="outreach", status="active", config=VALID_OUTREACH_CONFIG
        )
        overview = await campaign_stats.campaign_overview(session, test_tenant, campaign.id)

    assert overview["kind"] == "outreach"
    assert "rollup" in overview
    assert "member_states" in overview
    assert "quota" in overview
    assert overview["quota"]["daily_limit"] == 10


async def test_campaign_overview_surfaces_completion_report_when_present(
    test_session_factory, test_tenant
):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(
            session, test_tenant,
            insights={"completion_report": {"summary": "240 sent, 18 replies"}},
        )
        overview = await campaign_stats.campaign_overview(session, test_tenant, campaign.id)

    assert overview["completion_report"] == {"summary": "240 sent, 18 replies"}


async def test_campaign_overview_returns_none_for_missing_campaign(test_session_factory, test_tenant):
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        overview = await campaign_stats.campaign_overview(session, test_tenant, uuid.uuid4())

    assert overview is None


# ── list_campaign_replies ─────────────────────────────────────────────────


async def test_list_campaign_replies_returns_attributed_interactions(
    test_session_factory, test_tenant
):
    from backend.app.models import Contact, Interaction
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(session, test_tenant)
        contact = Contact(tenant_id=test_tenant.id, name="Jane Prospect", email="jane@x.com")
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

        session.add(Interaction(
            tenant_id=test_tenant.id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            channel="email",
            direction="inbound",
            subject="Re: Quick question",
            insights={"sentiment_score": 0.5, "summary": "x" * 400},
        ))
        await session.commit()

        result = await campaign_stats.list_campaign_replies(session, test_tenant, campaign.id)

    assert len(result["attributed_replies"]) == 1
    reply = result["attributed_replies"][0]
    assert reply["contact"] == "Jane Prospect"
    assert reply["subject"] == "Re: Quick question"
    assert reply["sentiment_score"] == 0.5
    # Summary snippet is capped so it can't blow the tool-result budget.
    assert len(reply["summary"]) <= 301


async def test_list_campaign_replies_reply_events_only_for_external_campaign(
    test_session_factory, test_tenant
):
    """External campaigns whose ESP pushed reply events without ingested
    interaction bodies must still report a reply count."""
    from backend.app.services import campaign_stats

    async with test_session_factory() as session:
        campaign = await _make_campaign(session, test_tenant, kind="external")
        recipient = await _make_recipient(session, test_tenant, campaign)
        await _make_event(session, test_tenant, campaign, "reply", recipient)
        await _make_event(session, test_tenant, campaign, "reply", recipient)

        result = await campaign_stats.list_campaign_replies(session, test_tenant, campaign.id)

    assert result["reply_events"] == 2
    assert result["attributed_replies"] == []
