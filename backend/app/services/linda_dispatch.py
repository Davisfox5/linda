"""Confirmed-proposal dispatch of an action step, for Ask LINDA.

This is the point where chat stops staging drafts and starts having
real-world effect: sending the step's email, writing its CRM note, or
booking its meeting.

**Why this is not a new safety surface.** The send/commit/schedule code is
``action_plan/dispatch.py`` — already shared by the manual per-step
endpoints (a rep clicking "Send") and the governed auto-executor (a policy
deciding). This module adds a third caller, not a third implementation,
and routes through the executor's own ``_dispatch_for_channel`` so channel
routing can't drift either.

**Why ``AUTO_EXECUTION_ENABLED`` does not gate it.** That flag governs
*unattended* dispatch: the auto-executor acting with nobody in the loop, on
a per-(tenant, action_class) policy that defaults to manual. Here a human
has read a proposal card and clicked Confirm. That is the human approval
the flag exists to require, not a bypass of it — the same relationship the
manual ``/send-email`` endpoint has to the flag, which is to say none.

**Where it is deliberately stricter than the manual endpoint.** The manual
endpoints dispatch whatever state the step is in, because a rep is looking
at the rendered artifact when they click. A Linda user is approving
Linda's *description* of the action, so this path also enforces the
auto-executor's pre-flight checks:

* the plan is active and the step is actually actionable — no re-sending a
  ``done`` step;
* an artifact exists and has no ``unfilled_slots`` — an artifact with
  unfilled slots still contains literal ``{{placeholders}}``, and sending
  that to a customer is a visible, unrecoverable embarrassment;
* the channel is one the dispatcher can actually handle.

Every refusal explains itself so the user can fall back to doing it by
hand in the SPA.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import ActionPlan, ActionStep, Tenant

logger = logging.getLogger(__name__)

# Channels with a real dispatch path. Mirrors the auto-executor's
# ``_DISPATCHABLE_CHANNELS``: 'research' and 'document_send' produce
# artifacts a human reviews or downloads — there is no "send" for them.
DISPATCHABLE_CHANNELS = frozenset(
    {"email", "note", "system_write", "meeting", "phone_call"}
)

# States a step can be dispatched from. 'done', 'awaiting_response',
# 'skipped' and 'deleted' are excluded so a confirm can't duplicate a send.
DISPATCHABLE_STATES = frozenset({"ready", "blocked", "in_progress"})


async def load_step(
    db: AsyncSession, tenant: Tenant, step_id: Any
) -> Tuple[Optional[ActionPlan], Optional[ActionStep], Optional[str]]:
    """Load a step and its plan, both tenant-scoped. Returns
    ``(plan, step, error)`` — ``error`` is a user-facing string."""
    try:
        step_uuid = uuid.UUID(str(step_id))
    except (TypeError, ValueError):
        return None, None, "invalid step_id"

    step = (
        await db.execute(
            select(ActionStep).where(
                ActionStep.id == step_uuid, ActionStep.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if step is None:
        return None, None, "step not found"

    plan = (
        await db.execute(
            select(ActionPlan).where(
                ActionPlan.id == step.plan_id, ActionPlan.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        return None, step, "step's plan not found"
    return plan, step, None


async def preflight(
    db: AsyncSession, tenant: Tenant, plan: ActionPlan, step: ActionStep
) -> Optional[str]:
    """Return a user-facing refusal reason, or ``None`` if good to send."""
    from backend.app.services.action_plan.dispatch import latest_artifact_for_step

    channel = (step.recommended_channel or "").lower()
    if channel not in DISPATCHABLE_CHANNELS:
        return (
            "Step '%s' has channel '%s', which has no automatic send — it "
            "produces something a person reviews or delivers. Handle it from "
            "the action plan instead." % (step.title, channel or "none")
        )
    if plan.status != "active":
        return "That step's plan is '%s', not active." % plan.status
    if step.state not in DISPATCHABLE_STATES:
        return (
            "Step '%s' is already '%s' — nothing to send. Re-sending would "
            "duplicate it." % (step.title, step.state)
        )

    artifact = await latest_artifact_for_step(
        db, tenant_id=tenant.id, step_id=step.id
    )
    if artifact is None or not isinstance(artifact.payload, dict):
        return (
            "Step '%s' has no generated content yet, so there's nothing to "
            "send." % step.title
        )
    unfilled = artifact.payload.get("unfilled_slots") or []
    if unfilled:
        return (
            "Step '%s' still has unfilled placeholders (%s). Sending it now "
            "would deliver the literal placeholder text — fill those in from "
            "the action plan first."
            % (step.title, ", ".join(str(u) for u in unfilled)[:200])
        )
    return None


async def preview(
    db: AsyncSession, tenant: Tenant, step_id: Any
) -> Dict[str, Any]:
    """Build the proposal preview for a step dispatch.

    Runs the same pre-flight the confirm will, so an un-sendable step is
    refused *before* the user is shown a Confirm button — the Tier 0 bug
    this whole effort started from was exactly that mismatch.
    """
    plan, step, error = await load_step(db, tenant, step_id)
    if error is not None:
        return {"error": error}

    blocker = await preflight(db, tenant, plan, step)
    if blocker is not None:
        return {"error": blocker}

    from backend.app.services.action_plan.dispatch import latest_artifact_for_step

    artifact = await latest_artifact_for_step(
        db, tenant_id=tenant.id, step_id=step.id
    )
    payload = artifact.payload if artifact else {}
    channel = (step.recommended_channel or "").lower()

    what_happens = {
        "email": "sends this email from the tenant's connected mailbox",
        "note": "writes this note to the connected CRM",
        "system_write": "runs this write against the connected CRM",
        "meeting": "books a calendar event and invites the participants",
        "phone_call": "books a calendar event for the call",
    }[channel]

    return {
        "step_id": str(step.id),
        "plan_id": str(plan.id),
        "step_title": step.title,
        "plan_goal": plan.goal,
        "channel": channel,
        "on_confirm": what_happens,
        # The rendered artifact is what actually goes out — show it, so the
        # user confirms the real content and not a paraphrase of it.
        "content": {
            k: v
            for k, v in payload.items()
            if k in ("subject", "body", "opening_line", "bullets", "closing_line", "operation")
        },
    }


async def execute(
    db: AsyncSession, tenant: Tenant, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Dispatch a confirmed step. Raises ``HTTPException`` on refusal.

    Returns a dict of provider identifiers for the audit trail.
    """
    from fastapi import HTTPException

    plan, step, error = await load_step(db, tenant, payload.get("step_id"))
    if error is not None:
        raise HTTPException(
            status_code=404 if "not found" in error else 422, detail=error
        )

    # Re-run pre-flight at confirm time: the proposal has a 24h TTL, and a
    # step can be sent, skipped, or re-generated in between.
    blocker = await preflight(db, tenant, plan, step)
    if blocker is not None:
        raise HTTPException(status_code=409, detail=blocker)

    from backend.app.services.action_plan.executor import _dispatch_for_channel

    channel = (step.recommended_channel or "").lower()
    try:
        result = await _dispatch_for_channel(
            db, channel=channel, tenant=tenant, plan=plan, step=step
        )
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
        logger.exception("linda step dispatch raised for step %s", step.id)
        raise HTTPException(status_code=502, detail=str(exc)[:500])

    if result is None or not getattr(result, "success", False):
        detail = getattr(result, "error", None) or "dispatch failed"
        logger.warning(
            "linda step dispatch failed for step %s (channel=%s): %s",
            step.id, channel, detail,
        )
        raise HTTPException(status_code=502, detail=str(detail)[:500])

    logger.info(
        "linda step dispatch: step=%s channel=%s provider=%s new_state=%s",
        step.id, channel, getattr(result, "provider", None), result.new_state,
    )
    return {
        "channel": channel,
        "provider": getattr(result, "provider", None),
        "new_state": result.new_state,
        "provider_message_id": getattr(result, "provider_message_id", None),
        "external_id": getattr(result, "external_id", None),
        "event_id": getattr(result, "event_id", None),
        "email_send_id": (
            str(getattr(result, "email_send_id", None))
            if getattr(result, "email_send_id", None)
            else None
        ),
    }
