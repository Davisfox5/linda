"""Ask Linda chat API — SSE streaming + write-proposal confirm/cancel."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import ActionItem, Tenant, User, WriteProposal
from backend.app.plans import require_feature
from backend.app.services.linda_agent import (
    AgentContext,
    get_or_create_conversation,
    run_chat_turn,
)
from backend.app.services.chat_rate_limiter import LindaRateLimiter

logger = logging.getLogger(__name__)
router = APIRouter()

_rate_limiter = LindaRateLimiter()


def _require_not_white_label(tenant: Tenant) -> None:
    if getattr(tenant, "is_white_label", False):
        # White-label tenants get 404 — the feature is invisible, not "forbidden".
        raise HTTPException(status_code=404, detail="Not found")


async def _resolve_current_user(
    request: Request, db: AsyncSession, tenant: Tenant
) -> Optional[User]:
    """Best-effort resolve the authenticated user for logging on messages.

    Tries the principal (works for session JWT + Clerk JWT). Returns
    None for tenant-API-key callers — that's the right behaviour: chat
    messages from key holders are recorded as tenant-attributed only.
    Failures fall back to None rather than raising; chat itself has
    already passed get_current_tenant by the time we get here.
    """
    from backend.app.auth import get_current_principal as _principal

    try:
        principal = await _principal(request, db)
    except Exception:
        return None
    if principal.user is None or principal.tenant.id != tenant.id:
        return None
    return principal.user


# ── Schemas ────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: Optional[uuid.UUID] = None


class ProposalOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    kind: str
    payload: dict
    status: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: Optional[datetime] = None
    resulting_entity_id: Optional[uuid.UUID] = None

    model_config = {"from_attributes": True}


# ── SSE helper ─────────────────────────────────────────────────────────────


def _sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


# Keepalive cadence while waiting on the LLM — SSE comment frames stop
# intermediaries (nginx, fly-proxy) from idle-closing the connection.
_HEARTBEAT_INTERVAL_S = 15.0
# Hard server-side bound on total stream lifetime. Consumers (Flex) run a
# 120s client-side abort; expiring here first turns "hang until the client
# gives up" into a clean terminal `error` event.
_STREAM_LIFETIME_S = 120.0

_QUEUE_END = None  # sentinel — producer events are always dicts


async def _stream_chat(
    ctx: AgentContext, user_message: str, conversation_id: uuid.UUID
) -> AsyncIterator[str]:
    """Adapt ``run_chat_turn`` into SSE frames with termination guarantees:

    - every exit path ends in a terminal ``done`` or ``error`` event,
    - a ``: keep-alive`` comment is emitted every ~15s of producer silence,
    - total stream lifetime is bounded at 120s (clean ``error`` on expiry).

    The producer runs in a background task feeding a queue so heartbeat
    timeouts never cancel it mid-``__anext__``; commit/rollback happens
    only after the pump task has finished, keeping the shared session
    single-user.
    """
    yield _sse({"type": "conversation", "conversation_id": str(conversation_id)})

    queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
    pump_failure: List[BaseException] = []

    async def _pump() -> None:
        try:
            async for event in run_chat_turn(ctx, user_message):
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pump_failure.append(exc)
        finally:
            queue.put_nowait(_QUEUE_END)

    pump = asyncio.create_task(_pump())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STREAM_LIFETIME_S
    saw_terminal = False
    timed_out = False

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=min(_HEARTBEAT_INTERVAL_S, remaining)
                )
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is _QUEUE_END:
                break
            if event.get("type") in ("done", "error"):
                saw_terminal = True
            yield _sse(event)

        if timed_out:
            logger.warning(
                "chat stream exceeded %.0fs lifetime (conversation %s)",
                _STREAM_LIFETIME_S,
                conversation_id,
            )
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass
            await ctx.db.rollback()
            yield _sse(
                {
                    "type": "error",
                    "message": (
                        f"Stream exceeded the {int(_STREAM_LIFETIME_S)}s server "
                        "limit. Start a new request to continue."
                    ),
                }
            )
            return

        if pump_failure:
            logger.error("chat stream failed", exc_info=pump_failure[0])
            await ctx.db.rollback()
            yield _sse({"type": "error", "message": str(pump_failure[0])})
            return

        try:
            await ctx.db.commit()
        except Exception as exc:
            logger.exception("chat stream commit failed")
            await ctx.db.rollback()
            yield _sse({"type": "error", "message": str(exc)})
            return
        # run_chat_turn always ends with `done`; belt-and-braces so the
        # terminal-event guarantee survives producer refactors.
        if not saw_terminal:
            yield _sse({"type": "done"})
    finally:
        if not pump.done():
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/chat/ping")
async def chat_ping(
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, bool]:
    """Lightweight liveness check — 404 if the tenant is white-label, 200 otherwise."""
    _require_not_white_label(tenant)
    return {"ok": True}


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    # require_feature gates on tier flag + trial-expiry; supersedes get_current_tenant.
    tenant: Tenant = Depends(require_feature("ask_linda")),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Ask Linda. Streams SSE events: text deltas, tool_use, tool_result, proposal, done."""
    _require_not_white_label(tenant)

    rate = await _rate_limiter.check(str(tenant.id))
    if not rate.allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(rate.retry_after_s)},
        )

    user = await _resolve_current_user(request, db, tenant)
    conversation = await get_or_create_conversation(
        db, tenant, user, payload.conversation_id
    )
    ctx = AgentContext(db=db, tenant=tenant, user=user, conversation_id=conversation.id)

    return StreamingResponse(
        _stream_chat(ctx, payload.message, conversation.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for real-time flush
        },
    )


@router.post("/chat/proposals/{proposal_id}/confirm", response_model=ProposalOut)
async def confirm_proposal(
    proposal_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ProposalOut:
    _require_not_white_label(tenant)

    proposal = (
        await db.execute(
            select(WriteProposal).where(
                WriteProposal.id == proposal_id, WriteProposal.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal is {proposal.status}")

    if proposal.expires_at < datetime.now(timezone.utc):
        proposal.status = "expired"
        await db.commit()
        raise HTTPException(status_code=410, detail="Proposal has expired")

    resulting_id = await _execute_proposal(db, proposal, tenant)
    proposal.status = "confirmed"
    proposal.confirmed_at = datetime.now(timezone.utc)
    proposal.resulting_entity_id = resulting_id
    await db.commit()
    await db.refresh(proposal)
    return proposal


@router.post("/chat/proposals/{proposal_id}/cancel", response_model=ProposalOut)
async def cancel_proposal(
    proposal_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ProposalOut:
    _require_not_white_label(tenant)

    proposal = (
        await db.execute(
            select(WriteProposal).where(
                WriteProposal.id == proposal_id, WriteProposal.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal is {proposal.status}")

    proposal.status = "cancelled"
    await db.commit()
    await db.refresh(proposal)
    return proposal


# ── Proposal executor ──────────────────────────────────────────────────────


async def _execute_crm_update(
    db: AsyncSession, payload: Dict[str, Any], tenant: Tenant
) -> Optional[str]:
    """Really run a confirmed ``crm_update`` proposal against the CRM.

    Reuses the exact write path ``action_plan/dispatch.dispatch_step_commit``
    uses for ``system_write`` steps — provider selection, adapter load with
    token decrypt/refresh, then ``execute_operation`` — so chat writes and
    action-plan writes cannot drift apart. Returns the provider's id for
    the created record, when the adapter reports one.

    This previously returned ``None`` without doing anything, so the user
    got a Confirm button that silently did nothing.
    """
    from backend.app.models import Contact, Customer, Interaction
    from backend.app.services.crm.base import CrmCapabilityMissing, CrmError
    from backend.app.services.crm.writeback import (
        _load_writeback_adapter,
        _pick_provider_for_writeback,
    )

    operation = str(payload.get("operation") or "").strip()
    if not operation:
        # Pre-repair proposals used a {target, fields} shape no adapter
        # could execute. They can still be pending (24h TTL), so fail them
        # with an explanation rather than pretending they worked.
        raise HTTPException(
            status_code=422,
            detail=(
                "This CRM proposal predates the current format and can't be "
                "executed. Ask Linda to propose the change again."
            ),
        )

    op_payload = payload.get("payload")
    if not isinstance(op_payload, dict):
        op_payload = {}

    try:
        interaction_uuid = uuid.UUID(str(payload.get("interaction_id")))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422, detail="crm_update proposal requires an interaction_id"
        )

    interaction = (
        await db.execute(
            select(Interaction).where(
                Interaction.id == interaction_uuid,
                Interaction.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if interaction is None:
        raise HTTPException(status_code=404, detail="Interaction not found")

    provider = await _pick_provider_for_writeback(db, tenant, interaction)
    if provider is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No CRM integration connected. Connect HubSpot, Salesforce, "
                "or Pipedrive under Settings."
            ),
        )
    adapter = await _load_writeback_adapter(db, tenant.id, provider)
    if adapter is None:
        raise HTTPException(
            status_code=409, detail="Failed to instantiate the %s CRM adapter." % provider
        )

    contact_external_id: Optional[str] = None
    customer_external_id: Optional[str] = None
    if interaction.contact_id is not None:
        contact = await db.get(Contact, interaction.contact_id)
        if contact is not None:
            contact_external_id = contact.crm_id
            if contact.customer_id is not None:
                customer = await db.get(Customer, contact.customer_id)
                if customer is not None:
                    customer_external_id = customer.crm_id

    try:
        external_id = await adapter.execute_operation(
            operation=operation,
            payload=op_payload,
            contact_external_id=op_payload.get("contact_external_id")
            or contact_external_id,
            customer_external_id=op_payload.get("customer_external_id")
            or customer_external_id,
            deal_external_id=op_payload.get("deal_external_id"),
        )
    except CrmCapabilityMissing as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:500])
    except CrmError as exc:
        logger.warning("crm_update proposal failed on %s: %s", provider, exc)
        raise HTTPException(status_code=502, detail=str(exc)[:500])
    except Exception as exc:  # noqa: BLE001 — surfaced, never silently swallowed
        logger.exception("crm_update proposal failed")
        raise HTTPException(status_code=502, detail=str(exc)[:500])
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            logger.debug("CRM adapter close failed", exc_info=True)

    return str(external_id) if external_id else None


async def _execute_proposal(
    db: AsyncSession, proposal: WriteProposal, tenant: Tenant
) -> Optional[uuid.UUID]:
    """Dispatch a confirmed proposal to the real mutator. Returns the created entity id."""
    payload = proposal.payload or {}

    if proposal.kind == "action_item":
        interaction_id = payload.get("interaction_id")
        try:
            interaction_uuid = uuid.UUID(interaction_id) if interaction_id else None
        except ValueError:
            interaction_uuid = None
        if interaction_uuid is None:
            raise HTTPException(
                status_code=422,
                detail="action_item proposal requires an interaction_id",
            )
        assignee_id: Optional[uuid.UUID] = None
        if payload.get("assignee_email"):
            assignee_id = (
                await db.execute(
                    select(User.id).where(
                        User.tenant_id == tenant.id,
                        User.email == payload["assignee_email"],
                    )
                )
            ).scalar_one_or_none()
        due: Optional[date] = None
        if payload.get("due_date"):
            try:
                due = date.fromisoformat(payload["due_date"])
            except ValueError:
                due = None

        item = ActionItem(
            interaction_id=interaction_uuid,
            tenant_id=tenant.id,
            assigned_to=assignee_id,
            title=payload["title"],
            description=payload.get("description"),
            priority=payload.get("priority", "medium"),
            due_date=due,
            # Canonical enum is {open, done, dismissed} (api/action_items.py).
            # This used to write "pending", which the list endpoint's
            # status=open filter does not match — Linda-created items were
            # invisible in the SPA's Open view.
            status="open",
        )
        db.add(item)
        await db.flush()
        return item.id

    if proposal.kind == "email_draft":
        # Email send integration is out of scope for this commit — we stage the
        # draft on the related ActionItem's email_draft field so the UI can
        # surface it for send-from-Gmail on the action item page.
        interaction_id = payload.get("interaction_id")
        try:
            interaction_uuid = uuid.UUID(interaction_id) if interaction_id else None
        except ValueError:
            interaction_uuid = None
        if interaction_uuid is None:
            raise HTTPException(
                status_code=422,
                detail="email_draft proposal requires a valid interaction_id",
            )
        item = ActionItem(
            interaction_id=interaction_uuid,
            tenant_id=tenant.id,
            title=payload.get("subject") or "Follow-up email",
            description="Email draft proposed by Linda.",
            priority="medium",
            status="open",
            email_draft={
                "subject": payload.get("subject"),
                "body": payload.get("body"),
                "recipients": payload.get("recipients", []),
            },
        )
        db.add(item)
        await db.flush()
        return item.id

    if proposal.kind == "crm_update":
        external_id = await _execute_crm_update(db, payload, tenant)
        # Record what the write produced so the confirmed proposal is an
        # audit record rather than just a flag. Reassigning (rather than
        # mutating) the JSONB dict is what makes SQLAlchemy persist it.
        proposal.payload = dict(payload, crm_external_id=external_id)
        # The CRM's id is a provider string, not a LINDA UUID, so there is
        # no resulting_entity_id to return.
        return None

    if proposal.kind == "queue_bump_email":
        # Queue the next outreach touch for a prospect. The actual send
        # stays with the campaign scheduler (window + daily throttle) —
        # confirming here only approves/queues it.
        from backend.app.models import Campaign, Customer, OutreachMember

        try:
            prospect_uuid = uuid.UUID(str(payload.get("prospect_id")))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="queue_bump_email proposal requires a prospect_id",
            )
        customer = await db.get(Customer, prospect_uuid)
        if customer is None or customer.tenant_id != tenant.id:
            raise HTTPException(status_code=404, detail="Prospect not found")
        if customer.do_not_contact or customer.pipeline_status == "do_not_contact":
            raise HTTPException(
                status_code=409, detail="Prospect is marked do-not-contact"
            )

        stmt = (
            select(OutreachMember)
            .join(Campaign, Campaign.id == OutreachMember.campaign_id)
            .where(
                OutreachMember.tenant_id == tenant.id,
                OutreachMember.customer_id == prospect_uuid,
                Campaign.kind == "outreach",
                Campaign.status.in_(("active", "paused", "draft")),
                OutreachMember.state.in_(
                    ("in_sequence", "queued", "needs_approval", "draft_pending", "completed")
                ),
            )
            .order_by(OutreachMember.created_at.desc())
        )
        if payload.get("campaign_id"):
            try:
                stmt = stmt.where(
                    OutreachMember.campaign_id == uuid.UUID(str(payload["campaign_id"]))
                )
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="Invalid campaign_id")
        member = (await db.execute(stmt)).scalars().first()
        if member is None:
            raise HTTPException(
                status_code=404,
                detail="Prospect has no outreach enrollment a bump can attach to",
            )

        if payload.get("subject"):
            member.draft_subject = str(payload["subject"])[:400]
        if payload.get("body"):
            member.draft_body = str(payload["body"])
        if member.draft_subject and member.draft_body:
            member.draft_status = "approved"
            member.state = "queued"
            member.next_send_at = datetime.now(timezone.utc)
        else:
            # No draft text yet — put it through the normal generate →
            # approve flow rather than sending something empty.
            member.draft_status = None
            member.state = "draft_pending"
            from backend.app.tasks import outreach_generate_drafts

            try:
                outreach_generate_drafts.delay(
                    str(tenant.id), str(member.campaign_id), [str(member.id)]
                )
            except Exception:
                logger.debug("outreach_generate_drafts enqueue failed", exc_info=True)
        await db.flush()
        return member.id

    if proposal.kind == "action_plan":
        # Linda-proposed Action Plan: one Plan + one Step + one v1
        # artifact. Pipeline-synthesized plans (Step 14a in tasks.py)
        # are the multi-step path; this is the lightweight "Linda
        # creates a single follow-up" path. Both write to the same
        # tables so the canvas renders them identically.
        from backend.app.models import (
            ActionPlan,
            ActionStep,
            StepArtifact,
        )
        from backend.app.services.action_plan.domains import REGISTRY as DOMAIN_REGISTRY

        interaction_id = payload.get("interaction_id")
        try:
            interaction_uuid = (
                uuid.UUID(interaction_id) if interaction_id else None
            )
        except ValueError:
            interaction_uuid = None

        assignee_id: Optional[uuid.UUID] = None
        if payload.get("assignee_email"):
            assignee_id = (
                await db.execute(
                    select(User.id).where(
                        User.tenant_id == tenant.id,
                        User.email == payload["assignee_email"],
                    )
                )
            ).scalar_one_or_none()

        due: Optional[date] = None
        if payload.get("due_date"):
            try:
                due = date.fromisoformat(payload["due_date"])
            except ValueError:
                due = None

        domain = payload.get("domain")
        if domain not in DOMAIN_REGISTRY:
            domain = tenant.default_domain or "generic"

        channel = payload.get("channel") or "note"
        # Map channel -> artifact kind, matching the synthesizer's mapping.
        artifact_kind = {
            "email": "email",
            "phone_call": "script",
            "meeting": "meeting",
            "document_send": "email",
            "research": "research",
            "system_write": "system_write_payload",
            "note": "note",
        }.get(channel, "note")

        title = payload["title"]
        description = payload.get("description")
        intent = payload.get("intent") or description or title

        plan = ActionPlan(
            tenant_id=tenant.id,
            interaction_id=interaction_uuid,
            goal=title[:200],
            domain=domain,
            status="active",
            manually_created=True,
            procedures_applied=[],
            external_context_snapshot={},
        )
        db.add(plan)
        await db.flush()

        step = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            assigned_to=assignee_id,
            title=title[:255],
            description=description,
            intent=intent,
            priority=payload.get("priority", "medium"),
            due_date=due,
            recommended_channel=channel,
            participants=[],
            prep_artifacts=[],
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[],
            output_data={},
            role_in_plan="customer_endpoint",
            artifact_version=1,
            artifact_stale=False,
        )
        db.add(step)
        await db.flush()
        plan.customer_endpoint_step_id = step.id

        # Lightweight placeholder artifact - the user can regenerate or
        # edit from the UI. We don't call Call C here because the
        # confirmation endpoint should stay snappy; the regen scheduler
        # will refresh later if the step gets slot data.
        placeholder_payload: dict
        if artifact_kind == "email":
            placeholder_payload = {
                "subject": title,
                "body": description or "(Add body here)",
                "cc": [],
                "bcc": [],
                "unfilled_slots": [],
            }
        elif artifact_kind == "script":
            placeholder_payload = {
                "opening_line": "",
                "bullets": [description or title],
                "closing_line": "",
                "unfilled_slots": [],
            }
        else:
            placeholder_payload = {
                "body": description or title,
                "unfilled_slots": [],
            }
        db.add(
            StepArtifact(
                step_id=step.id,
                tenant_id=tenant.id,
                version=1,
                kind=artifact_kind,
                payload=placeholder_payload,
                model_tier=None,
            )
        )
        await db.flush()
        return plan.id

    raise HTTPException(status_code=422, detail=f"unknown proposal kind: {proposal.kind}")
