"""Cold-outreach API — prospects pipeline + LINDA-originated campaigns.

Prospects ARE Customer rows (``prospect_id == customer_id``): a prospect
is any customer with a non-NULL ``pipeline_status``. Campaigns are the
existing ``campaigns`` table with ``kind='outreach'`` (see models.py) —
the send engine lives in services/outreach/, driven by Celery beat.

Endpoints (mutations gated on the ``campaigns:write`` API-key scope):

Prospects
- POST  /prospects/import                  — bulk upsert, idempotent on (tenant, website domain)
- GET   /prospects                         — pipeline list with campaign membership
- GET   /prospects/{id}                    — one prospect
- GET   /prospects/{id}/timeline           — chronological interaction tree
- PATCH /prospects/{id}                    — manual status / do-not-contact
- POST  /prospects/{id}/opt-out            — manual DNC shortcut

Campaigns
- POST  /outreach/campaigns                — create (status=draft) + enroll prospects
- GET   /outreach/campaigns                — list with member-state rollups
- GET   /outreach/campaigns/{id}           — detail + quota state
- GET   /outreach/campaigns/{id}/stats     — header + rollup (+ funnel/quota for outreach kind; also serves external campaigns)
- PATCH /outreach/campaigns/{id}           — partial update (guidance/template/window/limit/mode)
- POST  /outreach/campaigns/{id}/members   — enroll more prospects
- GET   /outreach/campaigns/{id}/members   — members incl. drafts
- POST  /outreach/campaigns/{id}/generate-drafts — 202, Celery fan-out
- POST  /outreach/campaigns/{id}/approve-drafts  — bulk approve
- PATCH /outreach/members/{member_id}      — edit / approve / reject / halt one member
- POST  /outreach/campaigns/{id}/activate  — validate + go live
- POST  /outreach/campaigns/{id}/pause     — stop sending (resume via activate)

Uploads
- POST   /outreach/uploads     — store a campaign attachment, returns the config ref
- GET    /outreach/email-logo  — current logo metadata + presigned preview URL
- POST   /outreach/email-logo  — upload/replace the tenant logo embedded in sends
- DELETE /outreach/email-logo  — remove the logo
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant, require_scope
from backend.app.db import get_db
from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    Contact,
    Customer,
    CustomerNote,
    Interaction,
    OutreachMember,
    Tenant,
)
from backend.app.services import campaign_stats
from backend.app.services.email.outbound import resolve_email_integration
from backend.app.services.outreach.common import (
    PIPELINE_STATUSES,
    normalize_domain,
    parse_config,
)
from backend.app.services.webhook_dispatcher import emit_event

logger = logging.getLogger(__name__)

router = APIRouter()

_IMPORT_MAX = 500
_ACTIVE_MEMBER_STATES = ("draft_pending", "needs_approval", "queued", "in_sequence")


# ── Schemas: prospects ─────────────────────────────────────────────────


class ProspectContactIn(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=50)
    instagram: Optional[str] = Field(None, max_length=200)


class ProspectImportIn(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=300)
    website: Optional[str] = Field(None, max_length=500)
    city: Optional[str] = Field(None, max_length=120)
    state: Optional[str] = Field(None, max_length=60)
    segment: Optional[str] = Field(None, max_length=120)
    current_software: Optional[str] = Field(None, max_length=300)
    # The one-line "why we win here" note the drafts personalize from.
    hook: Optional[str] = Field(None, max_length=2000)
    notes: Optional[str] = Field(None, max_length=4000)
    contact: Optional[ProspectContactIn] = None
    source: Optional[str] = Field(None, max_length=120)
    initial_status: Literal[
        "new", "queued", "contacted", "replied", "demo", "won", "lost", "do_not_contact"
    ] = "new"


class ProspectImportRequest(BaseModel):
    prospects: List[ProspectImportIn] = Field(..., min_length=1, max_length=_IMPORT_MAX)
    # Applied to rows that don't carry their own source tag.
    default_source: Optional[str] = Field(None, max_length=120)


class ProspectImportRowOut(BaseModel):
    prospect_id: uuid.UUID
    business_name: str
    domain: Optional[str]
    pipeline_status: Optional[str]
    contact_id: Optional[uuid.UUID]
    created: bool


class ProspectImportOut(BaseModel):
    created: int
    updated: int
    errors: List[Dict[str, Any]]
    prospects: List[ProspectImportRowOut]


class ProspectMembershipOut(BaseModel):
    campaign_id: uuid.UUID
    campaign_name: str
    member_id: uuid.UUID
    state: str
    touches_sent: int
    next_send_at: Optional[datetime]
    last_sent_at: Optional[datetime]


class ProspectOut(BaseModel):
    prospect_id: uuid.UUID
    business_name: str
    domain: Optional[str]
    pipeline_status: Optional[str]
    pipeline_status_changed_at: Optional[datetime]
    do_not_contact: bool
    city: Optional[str] = None
    state: Optional[str] = None
    segment: Optional[str] = None
    current_software: Optional[str] = None
    hook: Optional[str] = None
    source: Optional[str] = None
    instagram: Optional[str] = None
    primary_contact: Optional[Dict[str, Any]] = None
    memberships: List[ProspectMembershipOut] = Field(default_factory=list)
    last_interaction_at: Optional[datetime] = None


class ProspectListOut(BaseModel):
    items: List[ProspectOut]
    total: int
    limit: int
    offset: int


class ProspectPatchIn(BaseModel):
    pipeline_status: Optional[
        Literal["new", "queued", "contacted", "replied", "demo", "won", "lost", "do_not_contact"]
    ] = None
    do_not_contact: Optional[bool] = None
    reason: Optional[str] = Field(None, max_length=500)


class TimelineEntryOut(BaseModel):
    kind: Literal["interaction", "campaign_event", "note"]
    occurred_at: datetime
    # interaction
    interaction_id: Optional[uuid.UUID] = None
    channel: Optional[str] = None
    direction: Optional[str] = None
    subject: Optional[str] = None
    snippet: Optional[str] = None
    # campaign linkage (interactions + events)
    campaign_id: Optional[uuid.UUID] = None
    event_type: Optional[str] = None
    # note
    note_id: Optional[uuid.UUID] = None
    body: Optional[str] = None


class ProspectTimelineOut(BaseModel):
    prospect_id: uuid.UUID
    entries: List[TimelineEntryOut]


# ── Schemas: campaigns ─────────────────────────────────────────────────


class OutreachCampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
    # Validated against services.outreach.common.OutreachConfig — returned
    # 422 with pydantic details when malformed.
    config: Dict[str, Any]
    prospect_ids: List[uuid.UUID] = Field(default_factory=list, max_length=1000)


class MemberSkipOut(BaseModel):
    prospect_id: uuid.UUID
    reason: str


class OutreachCampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    status: str
    config: Dict[str, Any]
    sent_count: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    created_at: datetime
    member_states: Dict[str, int]
    quota: Optional[Dict[str, int]] = None
    skipped: List[MemberSkipOut] = Field(default_factory=list)


class OutreachMemberOut(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    prospect_id: uuid.UUID
    prospect_name: Optional[str]
    contact_email: Optional[str]
    state: str
    current_step: int
    touches_sent: int
    next_send_at: Optional[datetime]
    last_sent_at: Optional[datetime]
    replied_at: Optional[datetime]
    halt_reason: Optional[str]
    draft_subject: Optional[str]
    draft_body: Optional[str]
    draft_status: Optional[str]
    personalization: Dict[str, Any] = Field(default_factory=dict)


class MemberListOut(BaseModel):
    items: List[OutreachMemberOut]
    total: int
    limit: int
    offset: int


class MembersAddIn(BaseModel):
    prospect_ids: List[uuid.UUID] = Field(..., min_length=1, max_length=1000)


class GenerateDraftsIn(BaseModel):
    member_ids: Optional[List[uuid.UUID]] = None


class ApproveDraftsIn(BaseModel):
    member_ids: Optional[List[uuid.UUID]] = None
    all: bool = False


class MemberPatchIn(BaseModel):
    draft_subject: Optional[str] = Field(None, max_length=400)
    draft_body: Optional[str] = None
    action: Optional[Literal["approve", "reject", "halt"]] = None


# Partial-update shapes for PATCH /outreach/campaigns/{id}. Deliberately
# narrower than OutreachConfig — only the fields safe to change on a live
# campaign are accepted (extra="forbid" rejects the rest, so e.g.
# attachments or track_clicks can't slip through a merge unvalidated).


class CampaignStepPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset_days: Optional[int] = Field(None, ge=0, le=90)
    guidance: Optional[str] = None


class CampaignTemplatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: Optional[str] = Field(None, min_length=1, max_length=400)
    body: Optional[str] = Field(None, min_length=1)


class CampaignSendWindowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_hour: Optional[int] = Field(None, ge=0, le=23)
    end_hour: Optional[int] = Field(None, ge=1, le=24)
    timezone: Optional[str] = None
    days: Optional[List[int]] = None


class CampaignConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: Optional[List[CampaignStepPatch]] = None
    template: Optional[CampaignTemplatePatch] = None
    send_window: Optional[CampaignSendWindowPatch] = None
    daily_limit: Optional[int] = Field(None, ge=1, le=200)
    mode: Optional[str] = None


class OutreachCampaignPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=300)
    config: Optional[CampaignConfigPatch] = None


# ── Helpers ────────────────────────────────────────────────────────────


def _outreach_meta(customer: Customer) -> dict:
    return (customer.metadata_ or {}).get("outreach", {}) or {}


async def _emit(db: AsyncSession, tenant_id: uuid.UUID, event: str, data: dict) -> None:
    try:
        await emit_event(db, tenant_id, event, data)
    except Exception:
        logger.warning("webhook enqueue failed event=%s", event, exc_info=True)


async def _set_status_manual(
    db: AsyncSession,
    tenant: Tenant,
    customer: Customer,
    new_status: str,
    reason: str,
) -> None:
    old = customer.pipeline_status
    if new_status == old:
        return
    customer.pipeline_status = new_status
    customer.pipeline_status_changed_at = datetime.now(timezone.utc)
    if new_status == "do_not_contact":
        customer.do_not_contact = True
    await _emit(
        db, tenant.id, "prospect.status_changed",
        {
            "prospect_id": str(customer.id),
            "old_status": old,
            "new_status": new_status,
            "reason": reason,
            "campaign_id": None,
            "changed_at": customer.pipeline_status_changed_at.isoformat(),
        },
    )


async def _halt_active_members(
    db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, reason: str
) -> int:
    members = (
        (
            await db.execute(
                select(OutreachMember).where(
                    OutreachMember.tenant_id == tenant_id,
                    OutreachMember.customer_id == customer_id,
                    OutreachMember.state.in_(_ACTIVE_MEMBER_STATES),
                )
            )
        )
        .scalars()
        .all()
    )
    for m in members:
        m.state = "halted"
        m.halt_reason = reason
        m.next_send_at = None
    return len(members)


async def _get_prospect_or_404(
    db: AsyncSession, tenant: Tenant, prospect_id: uuid.UUID
) -> Customer:
    customer = await db.get(Customer, prospect_id)
    if customer is None or customer.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Prospect not found")
    if customer.pipeline_status is None:
        # Customers created outside the import (POST /customers, CRM sync,
        # entity resolution) may predate the pipeline row. Heal on read so
        # the prospect API never serves a null status and the list (which
        # filters on pipeline_status) picks the row up.
        customer.pipeline_status = (
            "do_not_contact" if customer.do_not_contact else "new"
        )
        customer.pipeline_status_changed_at = datetime.now(timezone.utc)
    return customer


async def _get_outreach_campaign_or_404(
    db: AsyncSession, tenant: Tenant, campaign_id: uuid.UUID
) -> Campaign:
    campaign = await db.get(Campaign, campaign_id)
    if (
        campaign is None
        or campaign.tenant_id != tenant.id
        or campaign.kind != "outreach"
    ):
        raise HTTPException(status_code=404, detail="Outreach campaign not found")
    return campaign


def _validated_config(raw: Dict[str, Any]) -> dict:
    try:
        return parse_config(raw).model_dump(mode="json")
    except ValidationError as exc:
        # ctx can carry the raw ValueError a custom validator raised
        # (e.g. the mode/provider checks) — not JSON serializable.
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        )


def _check_upload_keys(config: dict, tenant_id: uuid.UUID) -> None:
    """Campaign attachments may only reference this tenant's own uploads
    (the send engine enforces the same prefix as defense in depth)."""
    prefix = f"tenants/{tenant_id}/outreach/"
    for ref in config.get("attachments") or []:
        if not str(ref.get("s3_key") or "").startswith(prefix):
            raise HTTPException(
                status_code=422,
                detail="config.attachments entries must come from POST /outreach/uploads",
            )


# _member_states / _quota_state moved to
# backend/app/services/campaign_stats.py (member_states / quota_state) so
# the chat tools share the same definitions — see _campaign_out below.


async def _campaign_out(
    db: AsyncSession,
    tenant: Tenant,
    campaign: Campaign,
    skipped: Optional[List[MemberSkipOut]] = None,
) -> OutreachCampaignOut:
    return OutreachCampaignOut(
        id=campaign.id,
        name=campaign.name,
        kind=campaign.kind,
        status=campaign.status,
        config=campaign.config or {},
        sent_count=campaign.sent_count or 0,
        started_at=campaign.started_at,
        ended_at=campaign.ended_at,
        created_at=campaign.created_at,
        member_states=await campaign_stats.member_states(db, campaign.id),
        quota=await campaign_stats.quota_state(db, tenant.id, campaign),
        skipped=skipped or [],
    )


async def _enroll_prospects(
    db: AsyncSession,
    tenant: Tenant,
    campaign: Campaign,
    prospect_ids: List[uuid.UUID],
) -> tuple:
    """Create members for prospects. Returns (added, [MemberSkipOut])."""
    added = 0
    skipped: List[MemberSkipOut] = []
    if not prospect_ids:
        return added, skipped

    existing = set(
        (
            await db.execute(
                select(OutreachMember.customer_id).where(
                    OutreachMember.campaign_id == campaign.id
                )
            )
        )
        .scalars()
        .all()
    )
    for pid in prospect_ids:
        if pid in existing:
            skipped.append(MemberSkipOut(prospect_id=pid, reason="already_enrolled"))
            continue
        customer = await db.get(Customer, pid)
        if customer is None or customer.tenant_id != tenant.id:
            skipped.append(MemberSkipOut(prospect_id=pid, reason="not_found"))
            continue
        if customer.do_not_contact or customer.pipeline_status == "do_not_contact":
            skipped.append(MemberSkipOut(prospect_id=pid, reason="do_not_contact"))
            continue
        contact = (
            await db.execute(
                select(Contact)
                .where(
                    Contact.tenant_id == tenant.id,
                    Contact.customer_id == pid,
                    Contact.email.is_not(None),
                )
                .order_by(Contact.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if contact is None:
            skipped.append(MemberSkipOut(prospect_id=pid, reason="no_contact_email"))
            continue
        db.add(
            OutreachMember(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                customer_id=pid,
                contact_id=contact.id,
                state="draft_pending",
            )
        )
        existing.add(pid)
        added += 1
    await db.flush()
    return added, skipped


# ── Prospect endpoints ─────────────────────────────────────────────────


@router.post("/prospects/import", response_model=ProspectImportOut)
async def import_prospects(
    body: ProspectImportRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Bulk prospect upsert, idempotent on (tenant, normalized website domain).

    Matching: normalized domain first. Only rows with NO usable domain fall
    back to case-insensitive business-name match — a row whose domain matches
    no existing prospect always creates a new one, because two different
    non-null domains are two different businesses even if they share a name
    (e.g. two unrelated "Results Personal Training" gyms in different
    states). Re-imports refresh the outreach metadata + fill missing contact
    info; they never reset ``pipeline_status`` on a prospect that has
    already advanced, and never resurrect a do-not-contact prospect.
    """
    created = 0
    updated = 0
    errors: List[Dict[str, Any]] = []
    out_rows: List[ProspectImportRowOut] = []

    for idx, row in enumerate(body.prospects):
        try:
            domain = normalize_domain(row.website)
            customer: Optional[Customer] = None
            if domain:
                customer = (
                    await db.execute(
                        select(Customer)
                        .where(
                            Customer.tenant_id == tenant.id,
                            func.lower(Customer.domain) == domain,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if customer is None and domain is None:
                # Name fallback only when the row carries no domain at all:
                # a non-null domain that matched nothing means a distinct
                # business, never a same-name merge.
                customer = (
                    await db.execute(
                        select(Customer)
                        .where(
                            Customer.tenant_id == tenant.id,
                            func.lower(Customer.name) == row.business_name.lower(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()

            is_new = customer is None
            if is_new:
                customer = Customer(
                    tenant_id=tenant.id,
                    name=row.business_name,
                    domain=domain,
                )
                db.add(customer)
                await db.flush()

            meta = dict(customer.metadata_ or {})
            outreach_meta = dict(meta.get("outreach") or {})
            for key, val in (
                ("city", row.city),
                ("state", row.state),
                ("segment", row.segment),
                ("current_software", row.current_software),
                ("hook", row.hook),
                ("instagram", row.contact.instagram if row.contact else None),
            ):
                if val is not None:
                    outreach_meta[key] = val
            if row.source or body.default_source:
                outreach_meta.setdefault("source", row.source or body.default_source)
            outreach_meta["imported_at"] = datetime.now(timezone.utc).isoformat()
            meta["outreach"] = outreach_meta
            customer.metadata_ = meta

            # Never resets progress: only NULL-status rows take the
            # initial status, and DNC always sticks.
            if customer.pipeline_status is None and not customer.do_not_contact:
                customer.pipeline_status = row.initial_status
                customer.pipeline_status_changed_at = datetime.now(timezone.utc)
                if row.initial_status == "do_not_contact":
                    customer.do_not_contact = True

            contact_id: Optional[uuid.UUID] = None
            if row.contact and row.contact.email:
                email_lower = row.contact.email.lower()
                contact = (
                    await db.execute(
                        select(Contact)
                        .where(
                            Contact.tenant_id == tenant.id,
                            func.lower(Contact.email) == email_lower,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if contact is None:
                    contact = Contact(
                        tenant_id=tenant.id,
                        email=email_lower,
                        name=row.contact.name,
                        phone=row.contact.phone,
                        customer_id=customer.id,
                    )
                    db.add(contact)
                    await db.flush()
                else:
                    if contact.customer_id is None:
                        contact.customer_id = customer.id
                    if row.contact.name and not contact.name:
                        contact.name = row.contact.name
                    if row.contact.phone and not contact.phone:
                        contact.phone = row.contact.phone
                contact_id = contact.id

            if row.notes:
                db.add(
                    CustomerNote(
                        tenant_id=tenant.id,
                        customer_id=customer.id,
                        body=row.notes[:4000],
                    )
                )

            created += 1 if is_new else 0
            updated += 0 if is_new else 1
            out_rows.append(
                ProspectImportRowOut(
                    prospect_id=customer.id,
                    business_name=customer.name,
                    domain=customer.domain,
                    pipeline_status=customer.pipeline_status,
                    contact_id=contact_id,
                    created=is_new,
                )
            )
        except Exception as exc:
            logger.exception("prospect import row %s failed", idx)
            errors.append({"index": idx, "error": f"{type(exc).__name__}: {exc}"})

    await db.flush()
    return ProspectImportOut(
        created=created, updated=updated, errors=errors, prospects=out_rows
    )


async def _prospect_out(
    db: AsyncSession, customer: Customer, memberships_by_customer: Dict[uuid.UUID, List[ProspectMembershipOut]],
    last_interaction_by_customer: Dict[uuid.UUID, datetime],
    primary_contact_by_customer: Dict[uuid.UUID, Contact],
) -> ProspectOut:
    meta = _outreach_meta(customer)
    contact = primary_contact_by_customer.get(customer.id)
    return ProspectOut(
        prospect_id=customer.id,
        business_name=customer.name,
        domain=customer.domain,
        pipeline_status=customer.pipeline_status,
        pipeline_status_changed_at=customer.pipeline_status_changed_at,
        do_not_contact=customer.do_not_contact,
        city=meta.get("city"),
        state=meta.get("state"),
        segment=meta.get("segment"),
        current_software=meta.get("current_software"),
        hook=meta.get("hook"),
        source=meta.get("source"),
        instagram=meta.get("instagram"),
        primary_contact=(
            {
                "id": str(contact.id),
                "name": contact.name,
                "email": contact.email,
                "phone": contact.phone,
            }
            if contact
            else None
        ),
        memberships=memberships_by_customer.get(customer.id, []),
        last_interaction_at=last_interaction_by_customer.get(customer.id),
    )


async def _prospect_page_context(
    db: AsyncSession, tenant: Tenant, customers: List[Customer]
) -> tuple:
    ids = [c.id for c in customers]
    memberships: Dict[uuid.UUID, List[ProspectMembershipOut]] = {}
    last_interaction: Dict[uuid.UUID, datetime] = {}
    primary_contact: Dict[uuid.UUID, Contact] = {}
    if not ids:
        return memberships, last_interaction, primary_contact

    rows = (
        await db.execute(
            select(OutreachMember, Campaign.name)
            .join(Campaign, Campaign.id == OutreachMember.campaign_id)
            .where(OutreachMember.customer_id.in_(ids))
            .order_by(OutreachMember.created_at.desc())
        )
    ).all()
    for member, campaign_name in rows:
        memberships.setdefault(member.customer_id, []).append(
            ProspectMembershipOut(
                campaign_id=member.campaign_id,
                campaign_name=campaign_name,
                member_id=member.id,
                state=member.state,
                touches_sent=member.touches_sent,
                next_send_at=member.next_send_at,
                last_sent_at=member.last_sent_at,
            )
        )

    li_rows = (
        await db.execute(
            select(Interaction.customer_id, func.max(Interaction.created_at))
            .where(
                Interaction.tenant_id == tenant.id,
                Interaction.customer_id.in_(ids),
            )
            .group_by(Interaction.customer_id)
        )
    ).all()
    for cid, latest in li_rows:
        last_interaction[cid] = latest

    contact_rows = (
        await db.execute(
            select(Contact)
            .where(
                Contact.tenant_id == tenant.id,
                Contact.customer_id.in_(ids),
                Contact.email.is_not(None),
            )
            .order_by(Contact.created_at.asc())
        )
    ).scalars()
    for contact in contact_rows:
        primary_contact.setdefault(contact.customer_id, contact)

    return memberships, last_interaction, primary_contact


@router.get("/prospects", response_model=ProspectListOut)
async def list_prospects(
    status: Optional[str] = Query(None, description="pipeline status filter"),
    campaign_id: Optional[uuid.UUID] = Query(None),
    q: Optional[str] = Query(None, description="search name/domain"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Outreach-managed accounts (pipeline_status set), newest change first."""
    if status is not None and status not in PIPELINE_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown status {status!r}")

    filters = [
        Customer.tenant_id == tenant.id,
        Customer.pipeline_status.is_not(None),
    ]
    if status is not None:
        filters.append(Customer.pipeline_status == status)
    if q:
        needle = f"%{q.lower()}%"
        filters.append(
            or_(
                func.lower(Customer.name).like(needle),
                func.lower(Customer.domain).like(needle),
            )
        )
    if campaign_id is not None:
        member_ids = select(OutreachMember.customer_id).where(
            OutreachMember.campaign_id == campaign_id
        )
        filters.append(Customer.id.in_(member_ids))

    total = (
        await db.execute(select(func.count(Customer.id)).where(*filters))
    ).scalar_one()
    customers = (
        (
            await db.execute(
                select(Customer)
                .where(*filters)
                .order_by(
                    Customer.pipeline_status_changed_at.desc().nullslast(),
                    Customer.name.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    memberships, last_interaction, primary_contact = await _prospect_page_context(
        db, tenant, customers
    )
    items = [
        await _prospect_out(db, c, memberships, last_interaction, primary_contact)
        for c in customers
    ]
    return ProspectListOut(items=items, total=int(total), limit=limit, offset=offset)


@router.get("/prospects/{prospect_id}", response_model=ProspectOut)
async def get_prospect(
    prospect_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    customer = await _get_prospect_or_404(db, tenant, prospect_id)
    memberships, last_interaction, primary_contact = await _prospect_page_context(
        db, tenant, [customer]
    )
    return await _prospect_out(
        db, customer, memberships, last_interaction, primary_contact
    )


@router.get("/prospects/{prospect_id}/timeline", response_model=ProspectTimelineOut)
async def prospect_timeline(
    prospect_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Chronological interaction tree: every touch on the prospect —
    outbound campaign sends, replies, calls, transcripts, notes, plus
    campaign events (bounces, opt-outs) that never became interactions.
    Newest first."""
    customer = await _get_prospect_or_404(db, tenant, prospect_id)
    entries: List[TimelineEntryOut] = []

    interactions = (
        (
            await db.execute(
                select(Interaction)
                .where(
                    Interaction.tenant_id == tenant.id,
                    Interaction.customer_id == customer.id,
                )
                .order_by(Interaction.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for it in interactions:
        entries.append(
            TimelineEntryOut(
                kind="interaction",
                occurred_at=it.created_at,
                interaction_id=it.id,
                channel=it.channel,
                direction=it.direction,
                subject=it.subject or it.title,
                snippet=(it.raw_text or "")[:280] or None,
                campaign_id=it.campaign_id,
            )
        )

    event_rows = (
        await db.execute(
            select(CampaignEvent)
            .join(
                CampaignRecipient,
                CampaignRecipient.id == CampaignEvent.recipient_id,
            )
            .where(
                CampaignEvent.tenant_id == tenant.id,
                CampaignRecipient.customer_id == customer.id,
                # Replies land as interactions already; keep the tree
                # deduplicated by only surfacing non-interaction events.
                CampaignEvent.event_type.in_(("bounce", "unsubscribe")),
            )
            .order_by(CampaignEvent.occurred_at.desc())
            .limit(limit)
        )
    ).scalars()
    for ev in event_rows:
        entries.append(
            TimelineEntryOut(
                kind="campaign_event",
                occurred_at=ev.occurred_at,
                campaign_id=ev.campaign_id,
                event_type=ev.event_type,
            )
        )

    notes = (
        (
            await db.execute(
                select(CustomerNote)
                .where(
                    CustomerNote.tenant_id == tenant.id,
                    CustomerNote.customer_id == customer.id,
                )
                .order_by(CustomerNote.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for note in notes:
        entries.append(
            TimelineEntryOut(
                kind="note",
                occurred_at=note.created_at,
                note_id=note.id,
                body=note.body[:1000],
            )
        )

    entries.sort(key=lambda e: e.occurred_at, reverse=True)
    return ProspectTimelineOut(prospect_id=customer.id, entries=entries[:limit])


@router.patch("/prospects/{prospect_id}", response_model=ProspectOut)
async def patch_prospect(
    prospect_id: uuid.UUID,
    body: ProspectPatchIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Manual pipeline transition and/or do-not-contact flag.

    Manual writes may set any status (unlike campaign-driven transitions,
    which never move a prospect backwards). Setting do_not_contact halts
    every active sequence for the prospect.
    """
    customer = await _get_prospect_or_404(db, tenant, prospect_id)
    reason = body.reason or "manual"

    if body.do_not_contact is True or body.pipeline_status == "do_not_contact":
        customer.do_not_contact = True
        halted = await _halt_active_members(db, tenant.id, customer.id, "manual_dnc")
        await _set_status_manual(db, tenant, customer, "do_not_contact", reason)
        await _emit(
            db, tenant.id, "outreach.email.opted_out",
            {
                "prospect_id": str(customer.id),
                "prospect_name": customer.name,
                "campaign_id": None,
                "member_id": None,
                "interaction_id": None,
                "source": "manual",
                "halted_sequences": halted,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    elif body.do_not_contact is False:
        customer.do_not_contact = False
        if body.pipeline_status:
            await _set_status_manual(db, tenant, customer, body.pipeline_status, reason)
    elif body.pipeline_status:
        await _set_status_manual(db, tenant, customer, body.pipeline_status, reason)

    await db.flush()
    memberships, last_interaction, primary_contact = await _prospect_page_context(
        db, tenant, [customer]
    )
    return await _prospect_out(
        db, customer, memberships, last_interaction, primary_contact
    )


@router.post("/prospects/{prospect_id}/opt-out", response_model=ProspectOut)
async def opt_out_prospect(
    prospect_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Shortcut for PATCH {do_not_contact: true} — mark DNC and halt
    every active sequence."""
    return await patch_prospect(
        prospect_id,
        ProspectPatchIn(do_not_contact=True, reason="manual_opt_out"),
        db=db,
        tenant=tenant,
        _scope=None,
    )


# ── Campaign endpoints ─────────────────────────────────────────────────


@router.post("/outreach/campaigns", response_model=OutreachCampaignOut, status_code=201)
async def create_outreach_campaign(
    body: OutreachCampaignCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Create a draft outreach campaign and (optionally) enroll prospects.

    The config is validated up front (422 on shape errors) — including
    the CAN-SPAM identity fields (sender name/business/physical address),
    which are required, not optional.
    """
    config = _validated_config(body.config)
    _check_upload_keys(config, tenant.id)
    campaign = Campaign(
        tenant_id=tenant.id,
        name=body.name,
        channel="email",
        kind="outreach",
        status="draft",
        subject=config["template"]["subject"],
        config=config,
    )
    db.add(campaign)
    await db.flush()
    _, skipped = await _enroll_prospects(db, tenant, campaign, body.prospect_ids)
    return await _campaign_out(db, tenant, campaign, skipped=skipped)


@router.get("/outreach/campaigns", response_model=List[OutreachCampaignOut])
async def list_outreach_campaigns(
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    filters = [Campaign.tenant_id == tenant.id, Campaign.kind == "outreach"]
    if status is not None:
        filters.append(Campaign.status == status)
    campaigns = (
        (
            await db.execute(
                select(Campaign).where(*filters).order_by(Campaign.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await _campaign_out(db, tenant, c) for c in campaigns]


@router.get("/outreach/campaigns/{campaign_id}", response_model=OutreachCampaignOut)
async def get_outreach_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    return await _campaign_out(db, tenant, campaign)


@router.get("/outreach/campaigns/{campaign_id}/stats")
async def get_campaign_stats(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Full analytics for one campaign — header + rollup always, plus the
    member funnel/quota for outreach-kind campaigns and the completion
    report when one has been generated. Works for both campaign kinds:
    unlike ``_get_outreach_campaign_or_404``, ``campaign_overview`` doesn't
    filter on ``kind`` — external campaigns are stats-only (no send
    engine touches them) but still fetchable here.
    """
    overview = await campaign_stats.campaign_overview(db, tenant, campaign_id)
    if overview is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return overview


@router.patch("/outreach/campaigns/{campaign_id}", response_model=OutreachCampaignOut)
async def patch_outreach_campaign(
    campaign_id: uuid.UUID,
    body: OutreachCampaignPatch,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Partial campaign update: name, per-step guidance, template
    subject/body, send window, daily limit, mode.

    Sequence *timing* is locked once any member has moved beyond
    draft_pending — from there next_send_at values already derive from
    the current offsets, so a step-count or offset_days change would
    silently reshuffle in-flight schedules. Everything else stays
    editable on an active campaign.
    """
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    if body.config is not None:
        patch = body.config.model_dump(exclude_unset=True)
        merged = dict(campaign.config or {})
        if "steps" in patch:
            old_steps = list(merged.get("steps") or [])
            merged["steps"] = [
                {**(old_steps[i] if i < len(old_steps) else {}), **step_patch}
                for i, step_patch in enumerate(patch["steps"])
            ]
        if "template" in patch:
            merged["template"] = {**(merged.get("template") or {}), **patch["template"]}
        if "send_window" in patch:
            merged["send_window"] = {
                **(merged.get("send_window") or {}),
                **patch["send_window"],
            }
        for key in ("daily_limit", "mode"):
            if key in patch:
                merged[key] = patch[key]

        # Validating the OLD config too keeps the timing comparison on
        # like-for-like shapes (defaults filled in) — and fails fast on a
        # stale/broken stored config, same as generate-drafts.
        old_config = _validated_config(dict(campaign.config or {}))
        new_config = _validated_config(merged)
        old_timing = [s["offset_days"] for s in old_config["steps"]]
        new_timing = [s["offset_days"] for s in new_config["steps"]]
        if old_timing != new_timing:
            in_flight = (
                await db.execute(
                    select(func.count(OutreachMember.id)).where(
                        OutreachMember.campaign_id == campaign.id,
                        OutreachMember.state != "draft_pending",
                    )
                )
            ).scalar_one()
            if int(in_flight):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Cannot change the step count or offset_days while "
                        f"{int(in_flight)} member(s) are beyond draft_pending — "
                        "sequence timing is already in flight"
                    ),
                )
        campaign.config = new_config
        campaign.subject = new_config["template"]["subject"]
    if body.name is not None:
        campaign.name = body.name
    await db.flush()
    return await _campaign_out(db, tenant, campaign)


@router.post("/outreach/campaigns/{campaign_id}/members", response_model=OutreachCampaignOut)
async def add_campaign_members(
    campaign_id: uuid.UUID,
    body: MembersAddIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    if campaign.status in ("completed", "archived"):
        raise HTTPException(status_code=409, detail="Campaign is no longer active")
    _, skipped = await _enroll_prospects(db, tenant, campaign, body.prospect_ids)
    # New members mean the campaign may be actionable again.
    if campaign.status == "completed":
        campaign.status = "active"
    return await _campaign_out(db, tenant, campaign, skipped=skipped)


@router.get("/outreach/campaigns/{campaign_id}/members", response_model=MemberListOut)
async def list_campaign_members(
    campaign_id: uuid.UUID,
    state: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    filters = [OutreachMember.campaign_id == campaign.id]
    if state is not None:
        filters.append(OutreachMember.state == state)
    total = (
        await db.execute(select(func.count(OutreachMember.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(OutreachMember, Customer.name, Contact.email)
            .join(Customer, Customer.id == OutreachMember.customer_id)
            .join(Contact, Contact.id == OutreachMember.contact_id, isouter=True)
            .where(*filters)
            .order_by(OutreachMember.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        OutreachMemberOut(
            id=m.id,
            campaign_id=m.campaign_id,
            prospect_id=m.customer_id,
            prospect_name=customer_name,
            contact_email=contact_email,
            state=m.state,
            current_step=m.current_step,
            touches_sent=m.touches_sent,
            next_send_at=m.next_send_at,
            last_sent_at=m.last_sent_at,
            replied_at=m.replied_at,
            halt_reason=m.halt_reason,
            draft_subject=m.draft_subject,
            draft_body=m.draft_body,
            draft_status=m.draft_status,
            personalization=m.personalization or {},
        )
        for m, customer_name, contact_email in rows
    ]
    return MemberListOut(items=items, total=int(total), limit=limit, offset=offset)


@router.post(
    "/outreach/campaigns/{campaign_id}/generate-drafts", status_code=202
)
async def generate_campaign_drafts(
    campaign_id: uuid.UUID,
    body: Optional[GenerateDraftsIn] = None,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Kick off per-prospect draft personalization (Celery, batch queue).

    Poll GET /outreach/campaigns/{id}/members?state=needs_approval to see
    drafts arrive (review mode); auto-mode members queue themselves.
    """
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    _validated_config(campaign.config)  # fail fast on a stale/broken config
    member_ids = [str(m) for m in (body.member_ids if body else None) or []]
    pending = (
        await db.execute(
            select(func.count(OutreachMember.id)).where(
                OutreachMember.campaign_id == campaign.id,
                OutreachMember.state.in_(("draft_pending", "needs_approval")),
            )
        )
    ).scalar_one()
    from backend.app.tasks import outreach_generate_drafts

    outreach_generate_drafts.delay(
        str(tenant.id), str(campaign.id), member_ids or None
    )
    return {"status": "queued", "members_pending_draft": int(pending)}


@router.post("/outreach/campaigns/{campaign_id}/approve-drafts")
async def approve_campaign_drafts(
    campaign_id: uuid.UUID,
    body: ApproveDraftsIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Approve ready drafts individually (member_ids) or in bulk (all=true).

    Approved members enter ``queued`` and the scheduler sends them inside
    the campaign's window/throttle once the campaign is active.
    """
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    if not body.all and not body.member_ids:
        raise HTTPException(
            status_code=422, detail="Pass member_ids or all=true"
        )
    stmt = select(OutreachMember).where(
        OutreachMember.campaign_id == campaign.id,
        OutreachMember.state == "needs_approval",
        OutreachMember.draft_status == "ready",
    )
    if not body.all:
        stmt = stmt.where(OutreachMember.id.in_(body.member_ids))
    members = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)
    for m in members:
        m.draft_status = "approved"
        m.state = "queued"
        if m.next_send_at is None:
            m.next_send_at = now
    await db.flush()
    return {"approved": len(members)}


@router.patch("/outreach/members/{member_id}", response_model=OutreachMemberOut)
async def patch_member(
    member_id: uuid.UUID,
    body: MemberPatchIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Edit a member's current draft, and/or approve / reject / halt it.

    An edit on a ready draft keeps it awaiting approval; edit + action
    'approve' in one call is the common review-UI path. 'reject' sends
    the member back to draft_pending for regeneration. 'halt' removes
    the member from the campaign — it never sends and drops out of the
    review inbox and bulk approve — without touching the prospect's
    contactability elsewhere (prospect-level opt-out halts *every*
    campaign, which is too blunt for "just not this sequence").
    """
    member = await db.get(OutreachMember, member_id)
    if member is None or member.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Member not found")
    if body.action == "halt":
        # Halting is legal from any state that could still send (or
        # re-enter the send path); replied/completed are already done.
        if member.state in ("replied", "completed"):
            raise HTTPException(
                status_code=409,
                detail=f"Member in state {member.state!r} cannot be halted",
            )
    elif member.state not in ("draft_pending", "needs_approval", "queued"):
        raise HTTPException(
            status_code=409,
            detail=f"Member in state {member.state!r} has no editable draft",
        )

    if body.draft_subject is not None:
        member.draft_subject = body.draft_subject
    if body.draft_body is not None:
        member.draft_body = body.draft_body
    if body.action == "approve":
        if not (member.draft_subject and member.draft_body):
            raise HTTPException(status_code=422, detail="No draft to approve")
        member.draft_status = "approved"
        member.state = "queued"
        if member.next_send_at is None:
            member.next_send_at = datetime.now(timezone.utc)
    elif body.action == "reject":
        member.draft_status = None
        member.draft_subject = None
        member.draft_body = None
        member.state = "draft_pending"
    elif body.action == "halt":
        member.state = "halted"
        member.halt_reason = "removed_manually"
        member.next_send_at = None
        # Whatever draft exists is by definition unsent — drop it so the
        # member can't resurface anywhere drafts are listed or approved.
        member.draft_status = None
        member.draft_subject = None
        member.draft_body = None
    elif body.draft_subject is not None or body.draft_body is not None:
        if member.draft_status == "approved":
            # An edit after approval needs re-approval unless approved here.
            member.draft_status = "ready"
            member.state = "needs_approval"
            member.next_send_at = None
        elif member.draft_status is None:
            member.draft_status = "ready"
            member.state = "needs_approval"

    await db.flush()
    customer = await db.get(Customer, member.customer_id)
    contact = await db.get(Contact, member.contact_id) if member.contact_id else None
    return OutreachMemberOut(
        id=member.id,
        campaign_id=member.campaign_id,
        prospect_id=member.customer_id,
        prospect_name=customer.name if customer else None,
        contact_email=contact.email if contact else None,
        state=member.state,
        current_step=member.current_step,
        touches_sent=member.touches_sent,
        next_send_at=member.next_send_at,
        last_sent_at=member.last_sent_at,
        replied_at=member.replied_at,
        halt_reason=member.halt_reason,
        draft_subject=member.draft_subject,
        draft_body=member.draft_body,
        draft_status=member.draft_status,
        personalization=member.personalization or {},
    )


@router.post("/outreach/campaigns/{campaign_id}/activate", response_model=OutreachCampaignOut)
async def activate_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Go live: validates the config (CAN-SPAM identity included), checks
    a Gmail/Outlook integration is connected, and enqueues draft
    generation for any members still missing one. Sending starts on the
    next scheduler tick inside the send window."""
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    if campaign.status not in ("draft", "paused", "active"):
        raise HTTPException(
            status_code=409, detail=f"Cannot activate a {campaign.status} campaign"
        )
    try:
        config = parse_config(campaign.config)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        )

    integ = await resolve_email_integration(db, tenant.id, config.provider)
    if integ is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "No Gmail or Outlook integration connected — connect one "
                "via /oauth before activating an outreach campaign."
            ),
        )

    campaign.status = "active"
    pending = (
        await db.execute(
            select(func.count(OutreachMember.id)).where(
                OutreachMember.campaign_id == campaign.id,
                OutreachMember.state == "draft_pending",
            )
        )
    ).scalar_one()
    if pending:
        from backend.app.tasks import outreach_generate_drafts

        outreach_generate_drafts.delay(str(tenant.id), str(campaign.id), None)
    await db.flush()
    return await _campaign_out(db, tenant, campaign)


@router.post("/outreach/campaigns/{campaign_id}/pause", response_model=OutreachCampaignOut)
async def pause_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Stop sending immediately (members keep their state; activate resumes)."""
    campaign = await _get_outreach_campaign_or_404(db, tenant, campaign_id)
    if campaign.status not in ("active", "draft"):
        raise HTTPException(
            status_code=409, detail=f"Cannot pause a {campaign.status} campaign"
        )
    campaign.status = "paused"
    await db.flush()
    return await _campaign_out(db, tenant, campaign)


# ── Uploads: campaign attachments + the tenant email logo ──────────────

_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_LOGO_MAX_BYTES = 1 * 1024 * 1024
# Image types every mainstream mail client renders inline.
_LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}


def _safe_upload_name(filename: Optional[str]) -> str:
    cleaned = "".join(
        ch for ch in (filename or "") if ch.isprintable() and ch not in "/\\"
    ).strip()
    return cleaned or "file"


class OutreachUploadOut(BaseModel):
    s3_key: str
    filename: str
    content_type: Optional[str]
    size_bytes: int


@router.post("/outreach/uploads", response_model=OutreachUploadOut, status_code=201)
async def upload_outreach_attachment(
    file: UploadFile = File(...),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Store a file to attach to outreach sends. Put the returned ref
    into ``config.attachments`` when creating a campaign."""
    from backend.app.services.attachment_store import get_store

    store = get_store()
    if not store.available:
        raise HTTPException(status_code=503, detail="File storage is not configured")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(data) > _UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Attachment exceeds 10 MB")
    name = _safe_upload_name(file.filename)
    key = f"tenants/{tenant.id}/outreach/{uuid.uuid4().hex[:8]}-{name}"
    if not store.put_object(key, file.content_type, data):
        raise HTTPException(status_code=502, detail="Upload to file storage failed")
    return OutreachUploadOut(
        s3_key=key,
        filename=name,
        content_type=file.content_type,
        size_bytes=len(data),
    )


class EmailLogoOut(BaseModel):
    filename: str
    content_type: str
    size_bytes: Optional[int] = None
    # Short-lived presigned URL so the UI can render a preview.
    url: Optional[str] = None


@router.get("/outreach/email-logo", response_model=EmailLogoOut)
async def get_email_logo(
    tenant: Tenant = Depends(get_current_tenant),
):
    from backend.app.services.attachment_store import get_store

    meta = (getattr(tenant, "branding_config", None) or {}).get("email_logo")
    if not meta:
        raise HTTPException(status_code=404, detail="No email logo uploaded")
    return EmailLogoOut(
        filename=meta.get("filename") or "logo",
        content_type=meta.get("content_type") or "application/octet-stream",
        size_bytes=meta.get("size_bytes"),
        url=get_store().presigned_url(meta.get("s3_key") or ""),
    )


@router.post("/outreach/email-logo", response_model=EmailLogoOut, status_code=201)
async def upload_email_logo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    """Upload the tenant's email logo (PNG/JPEG/GIF, ≤ 1 MB). Outreach
    sends embed it inline at the bottom of the email unless the campaign
    template sets ``include_logo: false``. Uploading again replaces it."""
    from backend.app.services.attachment_store import get_store

    store = get_store()
    if not store.available:
        raise HTTPException(status_code=503, detail="File storage is not configured")
    content_type = (file.content_type or "").lower()
    if content_type not in _LOGO_TYPES:
        raise HTTPException(status_code=422, detail="Logo must be PNG, JPEG, or GIF")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Logo exceeds 1 MB")
    key = (
        f"tenants/{tenant.id}/branding/"
        f"{uuid.uuid4().hex[:8]}-logo{_LOGO_TYPES[content_type]}"
    )
    if not store.put_object(key, content_type, data):
        raise HTTPException(status_code=502, detail="Upload to file storage failed")

    cfg = dict(getattr(tenant, "branding_config", None) or {})
    old_key = (cfg.get("email_logo") or {}).get("s3_key")
    cfg["email_logo"] = {
        "s3_key": key,
        "filename": _safe_upload_name(file.filename),
        "content_type": content_type,
        "size_bytes": len(data),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    tenant.branding_config = cfg
    await db.flush()
    if old_key:
        store.delete(old_key)
    return EmailLogoOut(
        filename=cfg["email_logo"]["filename"],
        content_type=content_type,
        size_bytes=len(data),
        url=store.presigned_url(key),
    )


@router.delete("/outreach/email-logo", status_code=204)
async def delete_email_logo(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _scope: None = Depends(require_scope("campaigns:write")),
):
    from backend.app.services.attachment_store import get_store

    cfg = dict(getattr(tenant, "branding_config", None) or {})
    meta = cfg.pop("email_logo", None)
    if not meta:
        raise HTTPException(status_code=404, detail="No email logo uploaded")
    tenant.branding_config = cfg
    await db.flush()
    if meta.get("s3_key"):
        get_store().delete(meta["s3_key"])
