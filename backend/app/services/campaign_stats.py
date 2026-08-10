"""Shared campaign analytics — the source of truth for both the REST
campaign routers (``api/campaigns.py``, ``api/outreach.py``) and the Ask
LINDA chat tools (``services/linda_agent.py``).

Async, mirrors the router call convention ``(db: AsyncSession, tenant, ...)``.
Nothing here is tenant-gated beyond the explicit ``tenant_id`` filters below —
RLS also applies, but every query stays belt-and-suspenders scoped like the
routers it was extracted from.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    EmailSend,
    Interaction,
    OutreachMember,
    Tenant,
)
from backend.app.services.outreach.common import parse_config

# Cap on the reply-snippet length surfaced to the chat tool — protects the
# context window the same way other tool outputs do (search_interactions,
# search_sent_email).
_REPLY_SNIPPET_MAX = 300


async def list_campaigns(
    db: AsyncSession,
    tenant: Tenant,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Both campaign kinds, most-recent first.

    Ordered by ``COALESCE(started_at, created_at) DESC`` so draft/unsent
    campaigns (no ``started_at`` yet) still sort sensibly instead of being
    pushed to the bottom. This is the "most recent campaign" resolver for
    both the chat tool and any future manager-portal listing.
    """
    order_key = func.coalesce(Campaign.started_at, Campaign.created_at)
    stmt = select(Campaign).where(Campaign.tenant_id == tenant.id)
    if kind:
        stmt = stmt.where(Campaign.kind == kind)
    if status:
        stmt = stmt.where(Campaign.status == status)
    stmt = stmt.order_by(order_key.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "kind": c.kind,
            "status": c.status,
            "channel": c.channel,
            "subject": c.subject,
            "sent_count": c.sent_count or 0,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "ended_at": c.ended_at.isoformat() if c.ended_at else None,
        }
        for c in rows
    ]


async def compute_rollup(
    db: AsyncSession, tenant: Tenant, campaign_id: uuid.UUID
) -> Dict[str, Any]:
    """Sent/opens/clicks/replies/bounces/unsubs/conversions/sentiment.

    Body moved verbatim from the old ``api/campaigns.py:_compute_rollup``
    (rollup definitions are unchanged) — returns a plain dict so the router
    can still build its ``CampaignRollup`` pydantic model from it and the
    chat tool can use the dict directly.
    """
    counts_rows = (await db.execute(
        select(CampaignEvent.event_type, func.count(CampaignEvent.id))
        .where(CampaignEvent.campaign_id == campaign_id)
        .group_by(CampaignEvent.event_type)
    )).all()
    by_type = {row[0]: row[1] for row in counts_rows}

    # Human clicks: collapse repeats to one per (recipient, url) and skip
    # hits the click endpoint flagged as likely scanner prefetches. Events
    # ingested without click-tracking metadata (external ESPs) count too —
    # their suspected_bot is absent, i.e. not flagged.
    click_url = CampaignEvent.metadata_["url"].as_string()
    suspected_bot = CampaignEvent.metadata_["suspected_bot"].as_boolean()
    human_clicks_sq = (
        select(CampaignEvent.recipient_id, click_url.label("url"))
        .where(
            CampaignEvent.campaign_id == campaign_id,
            CampaignEvent.event_type == "click",
            or_(suspected_bot.is_(None), suspected_bot.is_(False)),
        )
        .group_by(CampaignEvent.recipient_id, click_url)
        .subquery()
    )
    unique_clicks = (
        await db.execute(select(func.count()).select_from(human_clicks_sq))
    ).scalar_one() or 0

    sent = (await db.execute(
        select(func.count(CampaignRecipient.id))
        .where(CampaignRecipient.campaign_id == campaign_id)
    )).scalar_one() or 0

    # Average sentiment across attributed inbound interactions.
    reply_scores = (await db.execute(
        select(Interaction.insights)
        .where(
            Interaction.tenant_id == tenant.id,
            Interaction.campaign_id == campaign_id,
            Interaction.direction == "inbound",
        )
    )).scalars().all()
    raw_scores: List[float] = []
    for payload in reply_scores:
        if not payload:
            continue
        s = payload.get("sentiment_score")
        try:
            raw_scores.append(float(s))
        except (TypeError, ValueError):
            continue
    avg = sum(raw_scores) / len(raw_scores) if raw_scores else None

    return {
        "sent": sent,
        "opens": by_type.get("open", 0),
        "clicks": by_type.get("click", 0),
        "unique_clicks": unique_clicks,
        "replies": by_type.get("reply", 0),
        "bounces": by_type.get("bounce", 0),
        "unsubscribes": by_type.get("unsubscribe", 0),
        "conversions": by_type.get("convert", 0),
        "reply_sentiment_avg": avg,
    }


async def member_states(db: AsyncSession, campaign_id: uuid.UUID) -> Dict[str, int]:
    """Outreach member counts by sequence state. Moved verbatim from
    ``api/outreach.py:_member_states``."""
    rows = (
        await db.execute(
            select(OutreachMember.state, func.count(OutreachMember.id))
            .where(OutreachMember.campaign_id == campaign_id)
            .group_by(OutreachMember.state)
        )
    ).all()
    return {state: int(count) for state, count in rows}


async def quota_state(
    db: AsyncSession, tenant_id: uuid.UUID, campaign: Campaign
) -> Optional[Dict[str, int]]:
    """Today's throttle counters (in the campaign's send-window tz).

    Moved verbatim from ``api/outreach.py:_quota_state``. Keeps the
    ``local_day_bounds_utc`` import function-local as it was in the
    router, to avoid an import cycle.
    """
    try:
        config = parse_config(campaign.config)
    except ValidationError:
        return None
    from backend.app.services.outreach.common import local_day_bounds_utc

    from backend.app.config import get_settings

    settings = get_settings()
    day_start, day_end = local_day_bounds_utc(config.send_window)

    async def _count(campaign_scoped: bool) -> int:
        stmt = select(func.count(EmailSend.id)).where(
            EmailSend.tenant_id == tenant_id,
            EmailSend.status == "sent",
            EmailSend.campaign_id.is_not(None),
            EmailSend.created_at >= day_start,
            EmailSend.created_at < day_end,
        )
        if campaign_scoped:
            stmt = stmt.where(EmailSend.campaign_id == campaign.id)
        return int((await db.execute(stmt)).scalar_one() or 0)

    daily_limit = config.daily_limit or settings.OUTREACH_DEFAULT_DAILY_LIMIT
    sent_today = await _count(True)
    tenant_sent_today = await _count(False)
    return {
        "daily_limit": daily_limit,
        "sent_today": sent_today,
        "remaining_today": max(0, daily_limit - sent_today),
        "tenant_daily_cap": settings.OUTREACH_TENANT_DAILY_SEND_CAP,
        "tenant_sent_today": tenant_sent_today,
    }


async def list_campaign_replies(
    db: AsyncSession, tenant: Tenant, campaign_id: uuid.UUID, limit: int = 10
) -> Dict[str, Any]:
    """Replies to one campaign, both attributed and raw.

    Two numbers, labeled separately so the model can explain gaps honestly:
    - ``attributed_replies``: inbound ``Interaction`` rows the ingest
      pipeline linked to this campaign (has sentiment/summary/subject —
      stamped by ``email_ingest/ingest.py``).
    - ``reply_events``: the raw ``CampaignEvent(event_type="reply")`` count,
      which external-kind campaigns get from ESP-pushed events even when no
      interaction body was ever ingested.
    """
    reply_events = (await db.execute(
        select(func.count(CampaignEvent.id)).where(
            CampaignEvent.campaign_id == campaign_id,
            CampaignEvent.tenant_id == tenant.id,
            CampaignEvent.event_type == "reply",
        )
    )).scalar_one() or 0

    rows = (
        await db.execute(
            select(Interaction)
            .where(
                Interaction.tenant_id == tenant.id,
                Interaction.campaign_id == campaign_id,
                Interaction.direction == "inbound",
            )
            .order_by(Interaction.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    attributed: List[Dict[str, Any]] = []
    for interaction in rows:
        insights = interaction.insights or {}
        summary = insights.get("summary")
        if summary and len(summary) > _REPLY_SNIPPET_MAX:
            summary = summary[:_REPLY_SNIPPET_MAX] + "…"
        contact = interaction.contact
        attributed.append(
            {
                "interaction_id": str(interaction.id),
                "contact": (contact.name or contact.email) if contact else interaction.from_address,
                "subject": interaction.subject,
                "summary": summary,
                "sentiment_score": insights.get("sentiment_score"),
                "occurred_at": interaction.created_at.isoformat() if interaction.created_at else None,
            }
        )

    return {
        "attributed_replies": attributed,
        "reply_events": int(reply_events),
    }


async def campaign_overview(
    db: AsyncSession, tenant: Tenant, campaign_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """Header + rollup always; member funnel + quota only for outreach
    campaigns. Composition used by the ``get_campaign_stats`` chat tool.
    Returns ``None`` if no campaign matches (caller renders not-found)."""
    campaign = (
        await db.execute(
            select(Campaign).where(
                Campaign.id == campaign_id, Campaign.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if campaign is None:
        return None

    overview: Dict[str, Any] = {
        "id": str(campaign.id),
        "name": campaign.name,
        "kind": campaign.kind,
        "status": campaign.status,
        "channel": campaign.channel,
        "subject": campaign.subject,
        "sent_count": campaign.sent_count or 0,
        "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
        "ended_at": campaign.ended_at.isoformat() if campaign.ended_at else None,
        "rollup": await compute_rollup(db, tenant, campaign_id),
    }

    if campaign.kind == "outreach":
        overview["member_states"] = await member_states(db, campaign.id)
        overview["quota"] = await quota_state(db, tenant.id, campaign)

    insights = campaign.insights or {}
    if "completion_report" in insights:
        overview["completion_report"] = insights["completion_report"]

    return overview
