"""Did Linda's actions actually work? — the chat-side outcome flywheel.

Two halves:

* **Recording** (:func:`record_decision`) runs on the request path when a
  proposal is confirmed or cancelled. It is deliberately best-effort: an
  analytics write must never fail a user's confirm.
* **Observing** (:func:`scan_all_tenants`, beat-driven) resolves each
  ``pending`` row from deterministic downstream state — an action item
  closing, an outreach member replying, a step completing. Nothing here is
  judged by a model: every signal is a real row changing, which is what
  makes this a *grounded* loop rather than the coherence trap of asking an
  LLM whether its own suggestion was good.

Outcome vocabulary:

``succeeded``  the action produced its intended downstream effect
``failed``     it was carried out and demonstrably didn't land (bounce, opt-out)
``rejected``   the human declined it — a cancel, or a dismissed item
``no_signal``  nothing resolved before the horizon; not evidence either way
``pending``    still inside the observation window

The distinction between ``rejected`` and ``no_signal`` matters: a cancel is
real evidence the proposal was wrong, while an action item nobody has
touched in three weeks is evidence of nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.app.models import LindaActionOutcome

logger = logging.getLogger(__name__)

# How long a pending outcome is given to resolve before it becomes
# ``no_signal``. Two weeks covers a follow-up cycle without holding rows
# open so long the flywheel has nothing to learn from.
OBSERVATION_HORIZON = timedelta(days=14)

# Kinds whose success is known the moment they're confirmed: the confirm
# path already executed them and would have raised otherwise.
IMMEDIATE_KINDS = frozenset({"crm_update"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ── Recording (request path) ───────────────────────────────────────────────


def build_outcome(
    proposal: Any,
    decision: str,
    resulting_entity_id: Optional[uuid.UUID] = None,
    extra_detail: Optional[Dict[str, Any]] = None,
) -> LindaActionOutcome:
    """The row for a decided proposal. Pure — callers own the session.

    ``extra_detail`` carries whatever the observer will need later that
    isn't recoverable from ``resulting_entity_id`` alone (e.g. the step id
    behind a ``step_dispatch``, whose effect lands on an existing row).
    """
    immediate = decision == "confirmed" and proposal.kind in IMMEDIATE_KINDS
    if decision in ("cancelled", "expired"):
        # A cancel is the clearest negative signal chat produces, and it
        # used to be thrown away entirely. An expiry is weaker — the user
        # may simply never have come back — so it is not counted against
        # the proposal.
        outcome = "rejected" if decision == "cancelled" else "no_signal"
        observed_at = _now()
    elif immediate:
        outcome, observed_at = "succeeded", _now()
    else:
        outcome, observed_at = "pending", None

    detail: Dict[str, Any] = dict(extra_detail or {})
    if immediate:
        detail["reason"] = "executed at confirm time"

    return LindaActionOutcome(
        tenant_id=proposal.tenant_id,
        proposal_id=proposal.id,
        conversation_id=getattr(proposal, "conversation_id", None),
        kind=proposal.kind,
        decision=decision,
        decided_at=_now(),
        resulting_entity_id=resulting_entity_id,
        outcome=outcome,
        outcome_detail=detail,
        observed_at=observed_at,
        observation_attempts=0,
    )


async def record_decision_async(
    db: Any,
    proposal: Any,
    decision: str,
    resulting_entity_id: Optional[uuid.UUID] = None,
    extra_detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Stage an outcome row inside the caller's transaction.

    Best-effort by design: the flywheel is analytics, and losing one row is
    strictly better than 500-ing a user's confirm. Failures are logged, not
    raised.
    """
    try:
        db.add(
            build_outcome(proposal, decision, resulting_entity_id, extra_detail)
        )
        await db.flush()
    except Exception:  # noqa: BLE001 — analytics must never break the write
        logger.warning(
            "linda_outcomes: failed to record %s for proposal %s",
            decision,
            getattr(proposal, "id", None),
            exc_info=True,
        )


# ── Observing (beat path, sync session) ────────────────────────────────────


def _observe_action_item(session: Session, row: LindaActionOutcome) -> Optional[Dict[str, Any]]:
    from backend.app.models import ActionItem

    if row.resulting_entity_id is None:
        return {"outcome": "no_signal", "detail": {"reason": "no resulting entity"}}
    item = (
        session.query(ActionItem)
        .filter(
            ActionItem.id == row.resulting_entity_id,
            ActionItem.tenant_id == row.tenant_id,
        )
        .first()
    )
    if item is None:
        return {"outcome": "no_signal", "detail": {"reason": "action item deleted"}}
    status = (item.status or "").lower()
    if status in ("done", "completed"):
        return {"outcome": "succeeded", "detail": {"action_item_status": status}}
    if status in ("dismissed", "rejected"):
        # The user took the item and then threw it away — a real negative.
        return {"outcome": "rejected", "detail": {"action_item_status": status}}
    return None


def _observe_action_plan(session: Session, row: LindaActionOutcome) -> Optional[Dict[str, Any]]:
    from backend.app.models import ActionPlan, ActionStep

    if row.resulting_entity_id is None:
        return {"outcome": "no_signal", "detail": {"reason": "no resulting entity"}}
    plan = (
        session.query(ActionPlan)
        .filter(
            ActionPlan.id == row.resulting_entity_id,
            ActionPlan.tenant_id == row.tenant_id,
        )
        .first()
    )
    if plan is None:
        return {"outcome": "no_signal", "detail": {"reason": "plan deleted"}}
    if plan.status == "abandoned":
        return {"outcome": "rejected", "detail": {"plan_status": "abandoned"}}
    if plan.status == "completed":
        return {"outcome": "succeeded", "detail": {"plan_status": "completed"}}
    done = (
        session.query(ActionStep)
        .filter(
            ActionStep.plan_id == plan.id,
            ActionStep.tenant_id == row.tenant_id,
            ActionStep.state == "done",
        )
        .count()
    )
    if done:
        return {"outcome": "succeeded", "detail": {"steps_done": done}}
    return None


def _observe_step_dispatch(session: Session, row: LindaActionOutcome) -> Optional[Dict[str, Any]]:
    from backend.app.models import ActionStep

    raw = (row.outcome_detail or {}).get("step_id")
    if not raw:
        return None
    try:
        # JSONB round-trips the id as a string; the column is UUID-typed,
        # so it has to be coerced back rather than compared as text.
        step_uuid = uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return {"outcome": "no_signal", "detail": {"reason": "unparseable step id"}}
    step = (
        session.query(ActionStep)
        .filter(ActionStep.id == step_uuid, ActionStep.tenant_id == row.tenant_id)
        .first()
    )
    if step is None:
        return {"outcome": "no_signal", "detail": {"reason": "step deleted"}}
    if step.state == "done":
        return {"outcome": "succeeded", "detail": {"step_state": "done"}}
    if step.state in ("skipped", "deleted"):
        return {"outcome": "rejected", "detail": {"step_state": step.state}}
    # 'awaiting_response' is genuinely undecided — the send worked, the
    # reply hasn't come. Let the horizon settle it.
    return None


def _observe_queue_bump(session: Session, row: LindaActionOutcome) -> Optional[Dict[str, Any]]:
    from backend.app.models import OutreachMember

    if row.resulting_entity_id is None:
        return {"outcome": "no_signal", "detail": {"reason": "no member"}}
    member = (
        session.query(OutreachMember)
        .filter(
            OutreachMember.id == row.resulting_entity_id,
            OutreachMember.tenant_id == row.tenant_id,
        )
        .first()
    )
    if member is None:
        return {"outcome": "no_signal", "detail": {"reason": "member deleted"}}
    if member.state == "replied":
        return {"outcome": "succeeded", "detail": {"member_state": "replied"}}
    if member.state in ("bounced", "opted_out"):
        # Carried out and demonstrably didn't land.
        return {"outcome": "failed", "detail": {"member_state": member.state}}
    return None


_OBSERVERS = {
    "action_item": _observe_action_item,
    "email_draft": _observe_action_item,  # confirms into an ActionItem
    "action_plan": _observe_action_plan,
    "step_dispatch": _observe_step_dispatch,
    "queue_bump_email": _observe_queue_bump,
}


def observe_tenant(session: Session, tenant_id: uuid.UUID, limit: int = 200) -> Dict[str, int]:
    """Resolve this tenant's pending outcomes. Returns per-outcome counts."""
    counts: Dict[str, int] = {"scanned": 0, "resolved": 0, "expired": 0}
    rows: List[LindaActionOutcome] = (
        session.query(LindaActionOutcome)
        .filter(
            LindaActionOutcome.tenant_id == tenant_id,
            LindaActionOutcome.outcome == "pending",
        )
        .order_by(LindaActionOutcome.decided_at)
        .limit(limit)
        .all()
    )
    now = _now()
    for row in rows:
        counts["scanned"] += 1
        row.observation_attempts = (row.observation_attempts or 0) + 1

        observer = _OBSERVERS.get(row.kind)
        verdict = None
        if observer is not None:
            try:
                verdict = observer(session, row)
            except Exception:  # noqa: BLE001 — one bad row can't stop the sweep
                logger.exception(
                    "linda_outcomes: observer failed for %s (kind=%s)", row.id, row.kind
                )
                continue

        if verdict is not None:
            row.outcome = verdict["outcome"]
            row.outcome_detail = dict(row.outcome_detail or {}, **verdict.get("detail", {}))
            row.observed_at = now
            counts["resolved"] += 1
            counts[row.outcome] = counts.get(row.outcome, 0) + 1
            continue

        decided = _aware(row.decided_at) or now
        if now - decided >= OBSERVATION_HORIZON:
            # Nothing resolved in the window. This is explicitly NOT counted
            # against the proposal — absence of a signal is not a failure.
            row.outcome = "no_signal"
            row.outcome_detail = dict(
                row.outcome_detail or {},
                reason="no downstream signal within %d days" % OBSERVATION_HORIZON.days,
            )
            row.observed_at = now
            counts["expired"] += 1
    return counts


def scan_all_tenants(session: Session) -> Dict[str, Any]:
    """Observe every tenant with pending outcomes.

    Per-tenant ``tenant_context`` + commit, so one tenant's failure can't
    poison the rest — same shape as the other cross-tenant sweeps.
    """
    from backend.app.models import Tenant
    from backend.app.tenant_ctx import tenant_context

    tenant_ids = [
        t[0]
        for t in session.query(LindaActionOutcome.tenant_id)
        .filter(LindaActionOutcome.outcome == "pending")
        .distinct()
        .all()
    ]

    totals: Dict[str, int] = {}
    tenants_scanned = 0
    for tenant_id in tenant_ids:
        try:
            with tenant_context(tenant_id, session):
                tenant = session.query(Tenant).filter(Tenant.id == tenant_id).first()
                if tenant is None:
                    continue
                counts = observe_tenant(session, tenant_id)
                session.commit()
            tenants_scanned += 1
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
        except Exception:  # noqa: BLE001 — never let one tenant kill the sweep
            session.rollback()
            logger.exception("linda_outcomes: scan failed for tenant %s", tenant_id)
    return {"tenants": tenants_scanned, **totals}


# ── Aggregate (what the flywheel reads) ────────────────────────────────────


def acceptance_summary(session: Session, tenant_id: uuid.UUID) -> Dict[str, Any]:
    """Per-kind decision + outcome counts for one tenant.

    The first consumer of the loop: which proposal kinds get confirmed, and
    of those, which actually land. A kind users routinely cancel is a tool
    description or a threshold problem, not a model problem.
    """
    from sqlalchemy import func as sa_func

    rows = (
        session.query(
            LindaActionOutcome.kind,
            LindaActionOutcome.decision,
            LindaActionOutcome.outcome,
            sa_func.count(LindaActionOutcome.id),
        )
        .filter(LindaActionOutcome.tenant_id == tenant_id)
        .group_by(
            LindaActionOutcome.kind,
            LindaActionOutcome.decision,
            LindaActionOutcome.outcome,
        )
        .all()
    )

    by_kind: Dict[str, Dict[str, Any]] = {}
    for kind, decision, outcome, count in rows:
        entry = by_kind.setdefault(
            kind,
            {"proposed": 0, "confirmed": 0, "cancelled": 0, "outcomes": {}},
        )
        entry["proposed"] += count
        if decision == "confirmed":
            entry["confirmed"] += count
        elif decision == "cancelled":
            entry["cancelled"] += count
        entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + count

    for entry in by_kind.values():
        if entry["proposed"]:
            entry["confirm_rate"] = round(entry["confirmed"] / entry["proposed"], 3)
        landed = entry["outcomes"].get("succeeded", 0)
        # Only resolved outcomes count in the denominator — pending and
        # no_signal are not evidence in either direction, and folding them
        # in would make every rate look worse the more recent the data is.
        resolved = landed + entry["outcomes"].get("failed", 0) + entry["outcomes"].get(
            "rejected", 0
        )
        if resolved:
            entry["success_rate"] = round(landed / resolved, 3)
    return {"tenant_id": str(tenant_id), "by_kind": by_kind}
