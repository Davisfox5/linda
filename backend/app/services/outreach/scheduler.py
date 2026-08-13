"""Cold-outreach sending engine (sync — runs inside Celery).

Every beat tick (``outreach_scheduler_tick`` in tasks.py):

  for each tenant (global table) → tenant_context → for each active
  outreach campaign → inside the send window? → how much daily quota is
  left (campaign limit AND tenant-wide cap)? → send up to
  OUTREACH_MAX_SENDS_PER_TICK due members, then surface due bumps.

Every send goes through the tenant's connected Gmail/Outlook OAuth (the
same transport as send-follow-up) and writes, atomically per member:

  - an ``EmailSend`` audit row (campaign_id + customer_id stamped),
  - a ``CampaignRecipient`` row carrying the RFC-822 Message-ID — the
    hook the email-ingest reply matcher keys on,
  - an outbound ``Interaction`` on the prospect (provider_message_id set,
    so the SENT-folder poller dedupes instead of double-writing),
  - member/prospect state transitions + the outreach webhooks.

Commit granularity is per member: a provider failure marks that member
``failed`` and moves on; it never rolls back earlier sends in the tick.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import (
    Campaign,
    CampaignRecipient,
    Contact,
    Customer,
    EmailSend,
    Integration,
    Interaction,
    OutreachMember,
    Tenant,
)
from backend.app.services.attachment_store import get_store
from backend.app.services.email.base import OutboundAttachment
from backend.app.services.email.outbound import (
    build_sender,
    close_sender,
    resolve_email_integration_sync,
)
from backend.app.services.outreach import copy_gate
from backend.app.services.outreach import drafts as drafts_mod
from backend.app.services.outreach.links import build_link_rewriter, persist_links
from backend.app.services.outreach.common import (
    OutreachConfig,
    advance_status,
    compose_footer,
    in_send_window,
    local_day_bounds_utc,
    parse_config,
    render_email_html,
    strip_markers,
)
from backend.app.services.webhook_dispatcher import dispatch_sync
from backend.app.tenant_ctx import tenant_context

logger = logging.getLogger(__name__)

# cid the HTML body uses to reference the tenant's inline logo.
_LOGO_CID = "tenant-logo"


def _load_email_logo(tenant: Tenant) -> Optional[OutboundAttachment]:
    """The tenant's uploaded email logo as an inline attachment, or None
    when no logo is configured / the object can't be fetched (a missing
    logo never blocks a send — the email just goes out without it)."""
    meta = (getattr(tenant, "branding_config", None) or {}).get("email_logo") or {}
    key = meta.get("s3_key")
    if not key:
        return None
    got = get_store().get(key)
    if got is None:
        logger.warning(
            "email logo unavailable for tenant %s (key=%s); sending without it",
            tenant.id, key,
        )
        return None
    data, fetched_type = got
    return OutboundAttachment(
        filename=meta.get("filename") or "logo",
        content_type=meta.get("content_type") or fetched_type,
        data=data,
        content_id=_LOGO_CID,
    )


# Member states the scheduler may still act on.
ACTIVE_MEMBER_STATES = (
    "draft_pending",
    "needs_approval",
    "queued",
    "in_sequence",
)


# ── Pipeline-status transitions (shared with the ingest hooks) ──────────


def set_pipeline_status_sync(
    session: Session,
    customer: Customer,
    proposed: str,
    *,
    reason: str,
    campaign_id: Optional[uuid.UUID] = None,
    manual: bool = False,
) -> Optional[str]:
    """Apply a pipeline-status transition and emit prospect.status_changed.

    Automatic transitions are monotonic (see common.advance_status);
    manual=True writes any valid status. Returns the new status when a
    write happened, else None.
    """
    old = customer.pipeline_status
    new = proposed if manual else advance_status(old, proposed)
    if new is None or new == old:
        return None
    customer.pipeline_status = new
    customer.pipeline_status_changed_at = datetime.now(timezone.utc)
    if new == "do_not_contact":
        customer.do_not_contact = True
    try:
        dispatch_sync(
            session,
            customer.tenant_id,
            "prospect.status_changed",
            {
                "prospect_id": str(customer.id),
                "old_status": old,
                "new_status": new,
                "reason": reason,
                "campaign_id": str(campaign_id) if campaign_id else None,
                "changed_at": customer.pipeline_status_changed_at.isoformat(),
            },
        )
    except Exception:
        logger.warning("prospect.status_changed webhook enqueue failed", exc_info=True)
    return new


def halt_members_for_customer(
    session: Session,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    reason: str,
    new_state: str = "halted",
    exclude_member_id: Optional[uuid.UUID] = None,
) -> int:
    """Stop every still-active sequence for a prospect (opt-out / manual DNC)."""
    members = (
        session.execute(
            select(OutreachMember).where(
                OutreachMember.tenant_id == tenant_id,
                OutreachMember.customer_id == customer_id,
                OutreachMember.state.in_(ACTIVE_MEMBER_STATES),
            )
        )
        .scalars()
        .all()
    )
    halted = 0
    for m in members:
        if exclude_member_id is not None and m.id == exclude_member_id:
            continue
        m.state = new_state
        m.halt_reason = reason
        m.next_send_at = None
        halted += 1
    return halted


# ── Quota accounting ────────────────────────────────────────────────────


def _sends_today(
    session: Session,
    tenant_id: uuid.UUID,
    config: OutreachConfig,
    campaign_id: Optional[uuid.UUID],
    now_utc: datetime,
) -> int:
    """Count of successful outreach sends in the window's local "today".

    campaign_id=None counts tenant-wide (any campaign) for the global cap.
    """
    day_start, day_end = local_day_bounds_utc(config.send_window, now_utc)
    # Counted on sent_at (stamped with the scheduler's logical clock),
    # not created_at (DB clock) — the two can disagree and the throttle
    # must follow the clock the window math uses.
    stmt = select(func.count(EmailSend.id)).where(
        EmailSend.tenant_id == tenant_id,
        EmailSend.status == "sent",
        EmailSend.campaign_id.is_not(None),
        EmailSend.sent_at >= day_start,
        EmailSend.sent_at < day_end,
    )
    if campaign_id is not None:
        stmt = stmt.where(EmailSend.campaign_id == campaign_id)
    return int(session.execute(stmt).scalar_one() or 0)


def quota_remaining(
    session: Session,
    tenant_id: uuid.UUID,
    campaign: Campaign,
    config: OutreachConfig,
    now_utc: datetime,
) -> int:
    settings = get_settings()
    campaign_limit = config.daily_limit or settings.OUTREACH_DEFAULT_DAILY_LIMIT
    campaign_used = _sends_today(session, tenant_id, config, campaign.id, now_utc)
    tenant_used = _sends_today(session, tenant_id, config, None, now_utc)
    return max(
        0,
        min(
            campaign_limit - campaign_used,
            settings.OUTREACH_TENANT_DAILY_SEND_CAP - tenant_used,
        ),
    )


# ── Draft generation (Celery task body) ─────────────────────────────────


def generate_drafts_for_campaign(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    *,
    member_ids: Optional[List[uuid.UUID]] = None,
) -> Dict[str, int]:
    """Fill in drafts for members that need one.

    review mode → needs_approval; auto mode → queued (approved) directly.
    Commits per member so a crash mid-fan-out keeps finished drafts.
    """
    config = parse_config(campaign.config)
    stmt = select(OutreachMember).where(
        OutreachMember.campaign_id == campaign.id,
        OutreachMember.state.in_(("draft_pending", "needs_approval")),
    )
    if member_ids:
        stmt = stmt.where(OutreachMember.id.in_(member_ids))
    members = session.execute(stmt).scalars().all()

    generated = 0
    failed = 0
    for member in members:
        if member.draft_status == "approved":
            continue
        customer = session.get(Customer, member.customer_id)
        if customer is None or customer.do_not_contact:
            member.state = "halted"
            member.halt_reason = "do_not_contact" if customer else "customer_missing"
            session.commit()
            continue
        try:
            draft = drafts_mod.generate_member_draft(campaign, config, member, customer)
        except Exception:
            logger.exception(
                "outreach draft generation failed member=%s campaign=%s",
                member.id, campaign.id,
            )
            failed += 1
            session.rollback()
            continue
        member.draft_subject = draft["subject"]
        member.draft_body = draft["body"]
        member.personalization = {k: v for k, v in draft["facts"].items() if v}
        # Auto mode may only self-approve drafts that clear the house
        # copy gate — a failing draft parks for human review instead,
        # with the failure list surfaced on the member.
        gate_failures: List[str] = []
        if config.mode == "auto" and get_settings().OUTREACH_COPY_GATE_ENABLED:
            gate_failures = copy_gate.evaluate_draft(
                draft["body"], customer.name
            )["failures"]
        if config.mode == "auto" and not gate_failures:
            member.draft_status = "approved"
            member.state = "queued"
            if member.next_send_at is None:
                member.next_send_at = datetime.now(timezone.utc)
        else:
            if gate_failures:
                member.personalization = {
                    **member.personalization,
                    "copy_gate_failures": gate_failures,
                }
            member.draft_status = "ready"
            member.state = "needs_approval"
        generated += 1
        session.commit()

    _emit_drafts_ready(session, tenant, campaign)
    return {"generated": generated, "failed": failed, "considered": len(members)}


def _emit_drafts_ready(session: Session, tenant: Tenant, campaign: Campaign) -> None:
    """Announce that a campaign has drafts waiting for a human.

    Emitted after the whole fan-out rather than per member, so a consumer
    gets one signal per batch instead of one per draft. The count is read
    back from the DB rather than accumulated in the loop: members parked by
    an earlier run are equally waiting, and a consumer asking "how many need
    review" wants the true queue depth, not this run's delta.

    No approval is implied. Drafts still require a human — see the
    no-autonomous-sending invariant in docs/ask-linda-integration.md.
    """
    try:
        pending = int(
            session.execute(
                select(func.count(OutreachMember.id)).where(
                    OutreachMember.campaign_id == campaign.id,
                    OutreachMember.state == "needs_approval",
                )
            ).scalar_one()
            or 0
        )
        if pending <= 0:
            return
        dispatch_sync(
            session,
            tenant.id,
            "outreach.drafts_ready",
            {
                "campaign_id": str(campaign.id),
                "name": campaign.name,
                "count": pending,
            },
        )
    except Exception:
        logger.warning("outreach.drafts_ready webhook enqueue failed", exc_info=True)


# ── The tick ────────────────────────────────────────────────────────────


def run_all_tenants(session: Session) -> Dict[str, Any]:
    """Beat entry point: tick each tenant that could possibly send.

    Two bulk queries replace the old per-tenant N+1 (one ``session.get``
    per tenant id, every 10 minutes, for every tenant in the install):

    * all Tenant rows in one SELECT (global table, no GUC needed);
    * the distinct set of tenants with a connected Gmail/Outlook
      integration — ``integrations`` is an RLS auth-bootstrap table, so
      this global read is allowed with no tenant bound (same pattern as
      the email poller). A tenant with no email integration can't send
      an outreach touch, so entering its tenant_context just to SELECT
      zero-result campaigns every tick is pure query noise.
    """
    tenants = session.execute(select(Tenant)).scalars().all()
    sendable_tenant_ids = {
        row
        for row in session.execute(
            select(Integration.tenant_id)
            .where(Integration.provider.in_(("google", "microsoft")))
            .distinct()
        ).scalars()
    }
    totals = {"tenants": 0, "campaigns": 0, "sent": 0, "failed": 0, "bumps_drafted": 0}
    for tenant in tenants:
        if tenant.id not in sendable_tenant_ids:
            continue
        with tenant_context(tenant.id, session):
            result = _run_tenant_tick(session, tenant)
        if result["campaigns"]:
            totals["tenants"] += 1
            for key in ("campaigns", "sent", "failed", "bumps_drafted"):
                totals[key] += result[key]
    return totals


def _run_tenant_tick(session: Session, tenant: Tenant) -> Dict[str, int]:
    campaigns = (
        session.execute(
            select(Campaign).where(
                Campaign.tenant_id == tenant.id,
                Campaign.kind == "outreach",
                Campaign.status == "active",
            )
        )
        .scalars()
        .all()
    )
    out = {"campaigns": 0, "sent": 0, "failed": 0, "bumps_drafted": 0}
    for campaign in campaigns:
        try:
            r = run_campaign_tick(session, tenant, campaign)
        except Exception:
            logger.exception("outreach tick failed campaign=%s", campaign.id)
            session.rollback()
            continue
        out["campaigns"] += 1
        out["sent"] += r["sent"]
        out["failed"] += r["failed"]
        out["bumps_drafted"] += r["bumps_drafted"]
    return out


def run_campaign_tick(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    now_utc: Optional[datetime] = None,
) -> Dict[str, int]:
    now_utc = now_utc or datetime.now(timezone.utc)
    config = parse_config(campaign.config)
    result = {"sent": 0, "failed": 0, "bumps_drafted": 0}

    # Surface due bumps regardless of the window — generating a bump
    # draft (review mode) isn't a send, and doing it outside the window
    # gives the human the whole day to approve before the window opens.
    result["bumps_drafted"] = _advance_due_bumps(session, campaign, config, now_utc)

    if not in_send_window(config.send_window, now_utc):
        _maybe_complete(session, tenant, campaign)
        return result

    budget = min(
        get_settings().OUTREACH_MAX_SENDS_PER_TICK,
        quota_remaining(session, tenant.id, campaign, config, now_utc),
    )
    if budget <= 0:
        _maybe_complete(session, tenant, campaign)
        return result

    due = (
        session.execute(
            select(OutreachMember)
            .where(
                OutreachMember.campaign_id == campaign.id,
                OutreachMember.state == "queued",
                OutreachMember.draft_status == "approved",
                OutreachMember.next_send_at <= now_utc,
            )
            .order_by(OutreachMember.next_send_at.asc())
            .limit(budget)
        )
        .scalars()
        .all()
    )
    if not due:
        _maybe_complete(session, tenant, campaign)
        return result

    integ = resolve_email_integration_sync(session, tenant.id, config.provider)
    if integ is None:
        logger.warning(
            "outreach campaign %s active but no email integration connected",
            campaign.id,
        )
        return result

    for member in due:
        ok = _send_member_touch(session, tenant, campaign, config, integ, member, now_utc)
        if ok:
            result["sent"] += 1
        else:
            result["failed"] += 1

    _maybe_complete(session, tenant, campaign)
    return result


def _advance_due_bumps(
    session: Session, campaign: Campaign, config: OutreachConfig, now_utc: datetime
) -> int:
    """Move in_sequence members whose bump is due into the draft flow."""
    due = (
        session.execute(
            select(OutreachMember).where(
                OutreachMember.campaign_id == campaign.id,
                OutreachMember.state == "in_sequence",
                OutreachMember.next_send_at.is_not(None),
                OutreachMember.next_send_at <= now_utc,
            )
        )
        .scalars()
        .all()
    )
    advanced = 0
    for member in due:
        if member.touches_sent >= config.max_touches or member.current_step >= len(
            config.steps
        ):
            member.state = "completed"
            member.next_send_at = None
            continue
        # A fresh step needs a fresh draft: back into the draft flow.
        member.draft_status = None
        member.draft_subject = None
        member.draft_body = None
        member.state = "draft_pending"
        advanced += 1
    if advanced:
        session.commit()
        # Reuse the draft generator inline — bump volume per tick is small.
        generate_drafts_for_campaign(session, session.get(Tenant, campaign.tenant_id), campaign)
    return advanced


def _send_member_touch(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    config: OutreachConfig,
    integ: Integration,
    member: OutreachMember,
    now_utc: datetime,
) -> bool:
    """Send one member's approved draft. Commits (or rolls back) itself."""
    customer = session.get(Customer, member.customer_id)
    contact = session.get(Contact, member.contact_id) if member.contact_id else None
    if customer is None or contact is None or not contact.email:
        member.state = "failed"
        member.halt_reason = "missing_contact_email"
        session.commit()
        return False
    if customer.do_not_contact or customer.pipeline_status == "do_not_contact":
        member.state = "halted"
        member.halt_reason = "do_not_contact"
        member.next_send_at = None
        session.commit()
        return False

    # Final backstop: every outgoing campaign email must clear the house
    # copy gate, even a human-approved (or human-edited) draft. A failing
    # draft is parked back in the review inbox — draft kept — instead of
    # going out.
    if get_settings().OUTREACH_COPY_GATE_ENABLED:
        gate = copy_gate.evaluate_draft(member.draft_body or "", customer.name)
        if not gate["pass"]:
            member.state = "needs_approval"
            member.draft_status = "ready"
            member.next_send_at = None
            member.personalization = {
                **(member.personalization or {}),
                "copy_gate_failures": gate["failures"],
            }
            session.commit()
            logger.warning(
                "outreach copy gate blocked send member=%s campaign=%s: %s",
                member.id, campaign.id, "; ".join(gate["failures"]),
            )
            return False
        if "copy_gate_failures" in (member.personalization or {}):
            member.personalization = {
                k: v
                for k, v in member.personalization.items()
                if k != "copy_gate_failures"
            }

    # Campaign file attachments must resolve before anything sends — a
    # promised attachment silently missing from a live email is worse
    # than holding the member back.
    outbound_attachments: List[OutboundAttachment] = []
    attachment_meta: List[dict] = []
    if config.attachments:
        store = get_store()
        key_prefix = f"tenants/{tenant.id}/outreach/"
        for ref in config.attachments:
            got = store.get(ref.s3_key) if ref.s3_key.startswith(key_prefix) else None
            if got is None:
                member.state = "failed"
                member.halt_reason = "attachment_unavailable"
                member.next_send_at = None
                session.commit()
                logger.warning(
                    "outreach attachment unavailable member=%s campaign=%s key=%s",
                    member.id, campaign.id, ref.s3_key,
                )
                return False
            data, fetched_type = got
            outbound_attachments.append(
                OutboundAttachment(
                    filename=ref.filename,
                    content_type=ref.content_type or fetched_type,
                    data=data,
                )
            )
            attachment_meta.append(
                {
                    "kind": "upload",
                    "s3_key": ref.s3_key,
                    "filename": ref.filename,
                    "content_type": ref.content_type or fetched_type,
                    "size_bytes": len(data),
                }
            )
    logo = _load_email_logo(tenant) if config.template.include_logo else None
    if logo is not None:
        outbound_attachments.append(logo)

    draft_text = member.draft_body or ""
    body = strip_markers(draft_text) + compose_footer(config.template)
    # Click tracking rewrites hrefs in the HTML part only — the plain
    # text part above keeps the original URLs. Tokens are collected here
    # and persisted only after the provider accepts the send.
    click_links: List[Tuple[str, str]] = []
    link_rewriter = (
        build_link_rewriter(click_links) if config.track_clicks else None
    )
    body_html = render_email_html(
        draft_text,
        config.template,
        logo_cid=(logo.content_id if logo else None),
        link_rewriter=link_rewriter,
    )
    subject = member.draft_subject or config.template.subject
    prior_ids = list(member.thread_message_ids or [])
    in_reply_to = prior_ids[-1] if prior_ids else None

    record = EmailSend(
        tenant_id=tenant.id,
        sender_user_id=None,
        provider=integ.provider,
        to_address=contact.email,
        subject=subject,
        body=body,
        attachments=attachment_meta,
        status="pending",
        campaign_id=campaign.id,
        customer_id=customer.id,
    )
    session.add(record)
    session.flush()

    sender = build_sender(integ, from_address_hint=None)

    async def _do_send():
        try:
            return await sender.send(
                to=[contact.email],
                subject=subject,
                body=body,
                body_html=body_html,
                attachments=outbound_attachments or None,
                in_reply_to=in_reply_to,
                references=prior_ids or None,
            )
        finally:
            await close_sender(sender)

    try:
        send_result = asyncio.run(_do_send())
    except Exception as exc:
        record.status = "failed"
        record.error = str(exc)[:500]
        member.state = "failed"
        member.halt_reason = "provider_error"
        session.commit()
        logger.warning(
            "outreach send failed member=%s campaign=%s: %s",
            member.id, campaign.id, exc,
        )
        return False

    record.status = "sent"
    record.provider_message_id = (
        send_result.provider_message_id or send_result.message_id
    )
    record.sent_at = now_utc

    recipient = CampaignRecipient(
        campaign_id=campaign.id,
        tenant_id=tenant.id,
        contact_id=contact.id,
        customer_id=customer.id,
        email_address=contact.email,
        rfc822_message_id=send_result.message_id,
        sent_at=now_utc,
        step=member.current_step,
    )
    session.add(recipient)

    if click_links:
        session.flush()  # assign recipient.id — the links key on it
        persist_links(
            session,
            click_links,
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            member_id=member.id,
            recipient_id=recipient.id,
        )

    # The outbound touch in the prospect's interaction tree. With a
    # provider_message_id (Gmail) the SENT-folder poller dedupes against
    # this row; Graph's /sendMail returns no id, so Outlook sends may
    # also surface via ingest — the timeline is customer-keyed, so both
    # rows still render in order.
    interaction = Interaction(
        tenant_id=tenant.id,
        contact_id=contact.id,
        customer_id=customer.id,
        campaign_id=campaign.id,
        channel="email",
        direction="outbound",
        source=integ.provider if integ.provider != "google" else "gmail",
        title=subject,
        subject=subject,
        raw_text=body,
        body_html=body_html,
        to_addresses=[contact.email],
        message_id=send_result.message_id,
        in_reply_to=in_reply_to,
        references=prior_ids,
        thread_id=(prior_ids[0] if prior_ids else send_result.message_id),
        provider_message_id=send_result.provider_message_id,
        is_internal=False,
        classification="sales",
        status="analyzed",
        insights={"outreach": {"campaign_id": str(campaign.id), "step": member.current_step}},
    )
    session.add(interaction)

    campaign.sent_count = (campaign.sent_count or 0) + 1
    if campaign.started_at is None:
        campaign.started_at = now_utc

    member.touches_sent += 1
    member.last_sent_at = now_utc
    if send_result.message_id:
        member.thread_message_ids = prior_ids + [send_result.message_id]
    member.current_step += 1
    if (
        member.current_step < len(config.steps)
        and member.touches_sent < config.max_touches
    ):
        offset = config.steps[member.current_step].offset_days
        member.state = "in_sequence"
        member.next_send_at = now_utc + timedelta(days=max(offset, 1))
    else:
        # Sequence exhausted. A late reply still attributes (the
        # recipient rows outlive the state machine) and flips this to
        # ``replied`` via the ingest hook.
        member.state = "completed"
        member.next_send_at = None

    set_pipeline_status_sync(
        session, customer, "contacted",
        reason="outreach_email_sent", campaign_id=campaign.id,
    )

    session.flush()
    try:
        dispatch_sync(
            session,
            tenant.id,
            "outreach.email.sent",
            {
                "prospect_id": str(customer.id),
                "prospect_name": customer.name,
                "campaign_id": str(campaign.id),
                "campaign_name": campaign.name,
                "member_id": str(member.id),
                "step": member.current_step - 1,
                "touches_sent": member.touches_sent,
                "to": contact.email,
                "subject": subject,
                "email_send_id": str(record.id),
                "interaction_id": str(interaction.id),
                "provider": integ.provider,
                "pipeline_status": customer.pipeline_status,
                "sent_at": now_utc.isoformat(),
            },
        )
    except Exception:
        logger.warning("outreach.email.sent webhook enqueue failed", exc_info=True)

    session.commit()
    return True


def _maybe_complete(session: Session, tenant: Tenant, campaign: Campaign) -> None:
    """Flip an active campaign to completed when no member can ever act again."""
    remaining = session.execute(
        select(func.count(OutreachMember.id)).where(
            OutreachMember.campaign_id == campaign.id,
            OutreachMember.state.in_(ACTIVE_MEMBER_STATES),
        )
    ).scalar_one()
    total = session.execute(
        select(func.count(OutreachMember.id)).where(
            OutreachMember.campaign_id == campaign.id
        )
    ).scalar_one()
    if total == 0 or remaining > 0 or campaign.status != "active":
        return
    campaign.status = "completed"
    campaign.ended_at = datetime.now(timezone.utc)

    def _count(state: str) -> int:
        return int(
            session.execute(
                select(func.count(OutreachMember.id)).where(
                    OutreachMember.campaign_id == campaign.id,
                    OutreachMember.state == state,
                )
            ).scalar_one()
            or 0
        )

    try:
        dispatch_sync(
            session,
            tenant.id,
            "campaign.completed",
            {
                "campaign_id": str(campaign.id),
                "name": campaign.name,
                "totals": {
                    "members": int(total),
                    "sent": int(campaign.sent_count or 0),
                    "replied": _count("replied"),
                    "bounced": _count("bounced"),
                    "opted_out": _count("opted_out"),
                    "completed_no_reply": _count("completed"),
                },
                "completed_at": campaign.ended_at.isoformat(),
            },
        )
    except Exception:
        logger.warning("campaign.completed webhook enqueue failed", exc_info=True)
    session.commit()
