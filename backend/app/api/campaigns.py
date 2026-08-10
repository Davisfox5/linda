"""Campaigns API — monitor external marketing campaigns.

We are not an ESP.  Clients push campaign metadata + recipients + events
in (or we pull them from an ESP in the future) so that AI analysis can
correlate campaign exposure with downstream sentiment and outcomes.

Endpoints:
- POST /campaigns                     — create (idempotent on external_id)
- GET  /campaigns                     — list for the tenant
- GET  /campaigns/{id}                — detail including rollup insights
- POST /campaigns/{id}/recipients     — bulk register recipients
- POST /campaigns/{id}/events         — bulk ingest engagement events
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    Contact,
    Tenant,
)
from backend.app.services import campaign_stats

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────


class CampaignCreate(BaseModel):
    name: str
    channel: Literal["email", "push", "other"] = "email"
    provider: Optional[str] = None
    external_id: Optional[str] = None
    subject: Optional[str] = None
    variant: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    channel: str
    provider: Optional[str]
    external_id: Optional[str]
    subject: Optional[str]
    variant: Optional[str]
    sent_count: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    insights: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class RecipientIn(BaseModel):
    email_address: EmailStr
    external_message_id: Optional[str] = None
    rfc822_message_id: Optional[str] = None
    sent_at: Optional[datetime] = None


class RecipientBulkIn(BaseModel):
    recipients: List[RecipientIn]


class EventIn(BaseModel):
    event_type: Literal["open", "click", "bounce", "unsubscribe", "reply", "convert"]
    email_address: Optional[EmailStr] = None
    external_message_id: Optional[str] = None
    rfc822_message_id: Optional[str] = None
    occurred_at: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Normalize naive input to UTC-aware (same convention as
        ``OutcomeEvent.occurred_at``) — a naive value would serialize
        without an offset and be re-parsed as local time downstream."""
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class EventBulkIn(BaseModel):
    events: List[EventIn]


class CampaignRollup(BaseModel):
    sent: int
    opens: int
    # Every recorded click event, scanner prefetches and repeats included.
    clicks: int
    # Deduped to one per (recipient, url) and excluding hits flagged as
    # likely scanner prefetches — the "first human click" number.
    unique_clicks: int
    replies: int
    bounces: int
    unsubscribes: int
    conversions: int
    reply_sentiment_avg: Optional[float]


class CampaignDetail(CampaignOut):
    rollup: CampaignRollup


# ── Endpoints ───────────────────────────────────────────


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(
    body: CampaignCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    # Idempotency: if an external_id is provided, upsert.
    if body.external_id:
        existing = (await db.execute(
            select(Campaign).where(
                Campaign.tenant_id == tenant.id,
                Campaign.external_id == body.external_id,
            )
        )).scalar_one_or_none()
        if existing is not None:
            # Update shallow fields only — don't trample a manual curation.
            for field in ("name", "subject", "variant", "started_at", "ended_at", "provider"):
                val = getattr(body, field)
                if val is not None:
                    setattr(existing, field, val)
            return existing

    campaign = Campaign(
        tenant_id=tenant.id,
        name=body.name,
        channel=body.channel,
        provider=body.provider,
        external_id=body.external_id,
        subject=body.subject,
        variant=body.variant,
        started_at=body.started_at,
        ended_at=body.ended_at,
        metadata_=body.metadata or {},
    )
    db.add(campaign)
    await db.flush()
    return campaign


@router.get("/campaigns", response_model=List[CampaignOut])
async def list_campaigns(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rows = (await db.execute(
        select(Campaign)
        .where(Campaign.tenant_id == tenant.id)
        .order_by(Campaign.started_at.desc().nullslast())
    )).scalars().all()
    return rows


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetail)
async def get_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    campaign = await _get_campaign_or_404(db, tenant, campaign_id)
    rollup = CampaignRollup(**await campaign_stats.compute_rollup(db, tenant, campaign_id))
    return CampaignDetail(
        **CampaignOut.model_validate(campaign).model_dump(),
        rollup=rollup,
    )


@router.post("/campaigns/{campaign_id}/recipients", status_code=201)
async def add_recipients(
    campaign_id: uuid.UUID,
    body: RecipientBulkIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    campaign = await _get_campaign_or_404(db, tenant, campaign_id)
    created = 0
    for r in body.recipients:
        # Try to bind to an existing Contact by email (tenant-scoped).
        contact = (await db.execute(
            select(Contact).where(
                Contact.tenant_id == tenant.id,
                Contact.email == r.email_address,
            )
        )).scalar_one_or_none()

        db.add(CampaignRecipient(
            campaign_id=campaign.id,
            tenant_id=tenant.id,
            contact_id=contact.id if contact else None,
            email_address=r.email_address,
            external_message_id=r.external_message_id,
            rfc822_message_id=r.rfc822_message_id,
            sent_at=r.sent_at,
        ))
        created += 1

    campaign.sent_count = (campaign.sent_count or 0) + created
    await db.flush()
    return {"created": created}


@router.post("/campaigns/{campaign_id}/events", status_code=201)
async def add_events(
    campaign_id: uuid.UUID,
    body: EventBulkIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    campaign = await _get_campaign_or_404(db, tenant, campaign_id)
    created = 0
    for e in body.events:
        recipient = None
        if e.external_message_id or e.rfc822_message_id or e.email_address:
            stmt = select(CampaignRecipient).where(
                CampaignRecipient.tenant_id == tenant.id,
                CampaignRecipient.campaign_id == campaign.id,
            )
            if e.external_message_id:
                stmt = stmt.where(CampaignRecipient.external_message_id == e.external_message_id)
            elif e.rfc822_message_id:
                stmt = stmt.where(CampaignRecipient.rfc822_message_id == e.rfc822_message_id)
            elif e.email_address:
                stmt = stmt.where(CampaignRecipient.email_address == e.email_address)
            recipient = (await db.execute(stmt)).scalars().first()

        db.add(CampaignEvent(
            campaign_id=campaign.id,
            tenant_id=tenant.id,
            recipient_id=recipient.id if recipient else None,
            contact_id=recipient.contact_id if recipient else None,
            event_type=e.event_type,
            occurred_at=e.occurred_at or datetime.now(timezone.utc),
            metadata_=e.metadata or {},
        ))
        created += 1
    await db.flush()
    return {"created": created}


# ── Helpers ─────────────────────────────────────────────


async def _get_campaign_or_404(
    db: AsyncSession, tenant: Tenant, campaign_id: uuid.UUID
) -> Campaign:
    campaign = (await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.tenant_id == tenant.id
        )
    )).scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


# _compute_rollup moved to backend/app/services/campaign_stats.py
# (campaign_stats.compute_rollup) so the chat tools share the same
# definitions. This router builds CampaignRollup from that dict.
