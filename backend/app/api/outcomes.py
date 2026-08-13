"""Proxy-outcome ingestion — production-grade webhook surface.

Hardened contract over the v1 endpoint:

- **``outcome_type`` is a ``Literal[...]``.** Unknown types 400 instead of
  being silently stored and ignored downstream.
- **Idempotency key (``event_id``) is required** when present in the payload.
  Repeated deliveries with the same ``(tenant_id, event_id)`` return 200
  without re-applying the event.  Payloads without an ``event_id`` are
  accepted (for backfill scripts and manual curl) but are rejected on
  retry by a hash fingerprint in the dead-letter log.
- **HMAC verification** — tenants with ``outcomes_hmac_secret`` set must
  include a valid ``X-Linda-Signature: sha256=<hex>`` header.
  Tenants without a secret accept unsigned calls (for gradual rollout).
- **Semantic floors** — ``occurred_at`` cannot be > 1 day in the future;
  if an ``interaction_id`` is referenced it must belong to the caller's
  tenant or the event is dead-lettered.
- **First-class ``customer_id`` (optional)** — validated against the
  interaction's resolved customer; a mismatch is rejected (422 on the
  single-event endpoint, dead-lettered ``customer_mismatch`` in batches)
  and never silently mis-attributed.  Persisted on the ingestion row.
- **Dead-letter log** — every dropped event is persisted to
  ``dropped_outcome_events`` with the failure reason so integrators can
  debug without losing data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    Contact,
    Customer,
    DroppedOutcomeEvent,
    Interaction,
    InteractionFeatures,
    OutcomeEventIngestion,
    Tenant,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Enum of accepted outcome types ───────────────────────────────────────
# Keep this list aligned with ``services/calibration.py`` —
# ``DEFAULT_CALIBRATION_CONFIGS`` consumes every key the calibrator can
# map onto a positive/negative outcome.

OutcomeType = Literal[
    "customer_replied",
    "customer_no_reply_72h",
    "customer_escalated",
    "contact_churned_30d",
    "contact_active_30d",
    "tenant_renewed",
    "tenant_upgraded",
    "tenant_churned",
    "action_item_closed",
    "action_item_closure_rate",
    "deal_won",
    "deal_lost",
]


# ── Request shapes ───────────────────────────────────────────────────────


class OutcomeEvent(BaseModel):
    """One observed downstream event, attributed to an interaction, a
    customer, or both.

    ``event_id`` is strongly recommended: if present, idempotency is
    enforced per ``(tenant_id, event_id)``.  Without it, the caller is
    responsible for avoiding duplicates.

    **Exactly one of ``interaction_id`` / ``customer_id`` is required.**
    Interaction-level events are the common case and feed the calibrator
    through ``InteractionFeatures.proxy_outcomes``.  Customer-level
    events exist for conversions with no originating call or email — a
    self-serve signup, for instance — where demanding an
    ``interaction_id`` would force the caller to invent one or drop the
    event entirely.  They are recorded and deduped, but deliberately do
    NOT write to ``proxy_outcomes``: attributing a customer-level
    conversion to whichever interactions happen to exist would teach the
    calibrator a causal link nobody verified.  See
    ``docs/outcomes.md`` for how the two levels differ downstream.

    ``customer_id`` alongside an ``interaction_id`` is first-class
    customer attribution: it is validated against the interaction's
    resolved customer (mismatches are rejected, never silently
    mis-attributed) and persisted on the ingestion row.
    """

    interaction_id: Optional[uuid.UUID] = None
    outcome_type: OutcomeType
    value: Optional[float] = None
    occurred_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    event_id: Optional[str] = Field(default=None, max_length=128)
    customer_id: Optional[uuid.UUID] = None

    @model_validator(mode="after")
    def _requires_a_subject(self) -> "OutcomeEvent":
        """An event with neither id attributes to nothing and is silently
        useless downstream — reject it at the door instead."""
        if self.interaction_id is None and self.customer_id is None:
            raise ValueError(
                "either interaction_id or customer_id is required"
            )
        return self

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Normalize naive input to UTC-aware.

        A naive timestamp would (a) blow up the aware comparison in
        ``_apply_events`` and (b) serialize without an offset, which
        browsers then parse as LOCAL time. Callers sending naive values
        mean UTC by convention.
        """
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class OutcomeBatch(BaseModel):
    events: List[OutcomeEvent]


class OutcomeAck(BaseModel):
    accepted: int
    duplicate: int
    dropped: int


# ── HMAC verification ────────────────────────────────────────────────────


def _verify_hmac(secret: Optional[str], body: bytes, signature_header: Optional[str]) -> bool:
    """Return True when the signature is valid, OR when the tenant has no
    secret configured (opt-in rollout).

    Expected header: ``X-Linda-Signature: sha256=<hex>``.
    """
    if not secret:
        return True
    if not signature_header:
        return False
    # Tolerate the optional ``sha256=`` prefix and any surrounding whitespace.
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256=") :]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


# ── Dead-letter helpers ──────────────────────────────────────────────────


async def _deadletter(
    db: AsyncSession,
    tenant_id: Optional[uuid.UUID],
    reason: str,
    payload: Any,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    row = DroppedOutcomeEvent(
        tenant_id=tenant_id,
        reason=reason,
        payload=_to_jsonable(payload),
        headers_snapshot=json.dumps(headers or {}, default=str)[:2000],
    )
    db.add(row)


def _to_jsonable(obj: Any) -> Dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return obj
    return {"value": str(obj)}


# ── Core apply ───────────────────────────────────────────────────────────


async def _customer_claim_error(
    db: AsyncSession, tenant_id: uuid.UUID, event: OutcomeEvent
) -> Optional[str]:
    """Validate an event's ``customer_id`` claim against the interaction.

    Returns a dead-letter reason string when the claim is invalid, else
    None.  A claim is invalid when the customer doesn't exist in the
    tenant (``customer_not_found``) or the interaction resolves to a
    different customer — directly or via its contact
    (``customer_mismatch``).  An interaction with no resolved customer
    accepts the claim: we can't disprove it, and rejecting would force
    callers back to metadata smuggling.
    """
    customer = (
        await db.execute(
            select(Customer.id).where(
                Customer.id == event.customer_id,
                Customer.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        return "customer_not_found"

    row = (
        await db.execute(
            select(Interaction.customer_id, Interaction.contact_id).where(
                Interaction.id == event.interaction_id,
                Interaction.tenant_id == tenant_id,
            )
        )
    ).one_or_none()
    if row is None:
        return "interaction_not_found"
    resolved_customer_id, contact_id = row

    if resolved_customer_id is not None:
        if resolved_customer_id != event.customer_id:
            return "customer_mismatch"
        return None
    if contact_id is not None:
        contact_customer_id = (
            await db.execute(
                select(Contact.customer_id).where(Contact.id == contact_id)
            )
        ).scalar_one_or_none()
        if (
            contact_customer_id is not None
            and contact_customer_id != event.customer_id
        ):
            return "customer_mismatch"
    return None


async def _apply_events(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    events: List[OutcomeEvent],
    headers_snapshot: Dict[str, str],
    *,
    strict_customer: bool = False,
) -> OutcomeAck:
    """Apply a batch of events.  Returns counts per outcome.

    For each event: validate semantic floors, check idempotency, update
    ``InteractionFeatures.proxy_outcomes``, record the ingestion row, or
    dead-letter on any failure.  Callers always get a meaningful count
    back — the only exception is ``strict_customer=True`` (single-event
    endpoint), where a ``customer_id`` claim that fails validation
    raises 422 after dead-lettering, instead of silently counting as
    dropped inside a 202.
    """
    now = datetime.now(timezone.utc)
    future_limit = now + timedelta(days=1)
    accepted = duplicate = dropped = 0

    for event in events:
        # Semantic floor: occurred_at cannot be in the far future.
        if event.occurred_at and event.occurred_at > future_limit:
            dropped += 1
            await _deadletter(db, tenant_id, "future_timestamp", event, headers_snapshot)
            continue

        if event.interaction_id is None:
            # Customer-level attribution — a conversion with no
            # originating interaction.  There is no InteractionFeatures
            # row to write to, but the customer must still exist in this
            # tenant: an orphan ingestion row keyed to a typo'd id is
            # worse than a dead-letter, because nothing ever joins to it
            # and the miss is invisible.
            owner = (
                await db.execute(
                    select(Customer.id).where(
                        Customer.id == event.customer_id,
                        Customer.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if owner is None:
                dropped += 1
                await _deadletter(
                    db, tenant_id, "customer_not_found", event, headers_snapshot
                )
                if strict_customer:
                    await db.commit()
                    raise HTTPException(
                        status_code=422,
                        detail="customer_id rejected: customer_not_found",
                    )
                continue
            features_row = None
        else:
            # Resolve interaction.  We re-check tenant scoping here so
            # cross-tenant writes die on the floor with a clear
            # dead-letter reason.
            stmt = select(InteractionFeatures).where(
                InteractionFeatures.interaction_id == event.interaction_id,
                InteractionFeatures.tenant_id == tenant_id,
            )
            features_row = (await db.execute(stmt)).scalar_one_or_none()
            if features_row is None:
                dropped += 1
                await _deadletter(
                    db, tenant_id, "interaction_not_found", event, headers_snapshot
                )
                continue

            # Optional first-class customer attribution — reject a claim
            # that contradicts the interaction's resolved customer rather
            # than silently mis-attributing the outcome.
            if event.customer_id is not None:
                claim_error = await _customer_claim_error(db, tenant_id, event)
                if claim_error is not None:
                    dropped += 1
                    await _deadletter(
                        db, tenant_id, claim_error, event, headers_snapshot
                    )
                    if strict_customer:
                        await db.commit()
                        raise HTTPException(
                            status_code=422,
                            detail=f"customer_id rejected: {claim_error}",
                        )
                    continue

        # Idempotency check.
        if event.event_id:
            existing_stmt = select(OutcomeEventIngestion).where(
                OutcomeEventIngestion.tenant_id == tenant_id,
                OutcomeEventIngestion.event_id == event.event_id,
            )
            existing = (await db.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                duplicate += 1
                continue

        # Apply to features.  Multiple events of the same outcome_type
        # append so late corrections (churned then retained, etc.) stay
        # visible to the calibrator.
        #
        # Customer-level events skip this entirely — see OutcomeEvent's
        # docstring.  They are durable and deduped via the ingestion row
        # below, but they do not become interaction-level evidence.
        if features_row is not None:
            outcomes = dict(features_row.proxy_outcomes or {})
            record = {
                "value": event.value,
                "occurred_at": (event.occurred_at or now).isoformat(),
                "metadata": event.metadata or {},
            }
            if event.customer_id is not None:
                record["customer_id"] = str(event.customer_id)
            existing_value = outcomes.get(event.outcome_type)
            if existing_value is None:
                outcomes[event.outcome_type] = record
            elif isinstance(existing_value, list):
                existing_value.append(record)
                outcomes[event.outcome_type] = existing_value
            else:
                outcomes[event.outcome_type] = [existing_value, record]
            features_row.proxy_outcomes = outcomes
            flag_modified(features_row, "proxy_outcomes")

        # Record the ingestion (enforces idempotency via unique index).
        ingestion = OutcomeEventIngestion(
            tenant_id=tenant_id,
            event_id=event.event_id or _autogen_event_id(event),
            outcome_type=event.outcome_type,
            interaction_id=event.interaction_id,
            customer_id=event.customer_id,
            payload=event.model_dump(mode="json"),
        )
        db.add(ingestion)
        try:
            await db.flush()
            accepted += 1
        except IntegrityError:
            await db.rollback()
            duplicate += 1

    return OutcomeAck(accepted=accepted, duplicate=duplicate, dropped=dropped)


def _autogen_event_id(event: OutcomeEvent) -> str:
    """Fingerprint an ``event_id``-less payload so retries dedupe.

    Hash of ``(subject, outcome_type, occurred_at)`` — if a caller
    re-sends the same event without an explicit ``event_id`` we still
    catch it.  Collisions across legitimately distinct events with the
    same key tuple are possible but expected to be vanishingly rare.

    The interaction-level key format is frozen: changing it would remint
    every fingerprint and let an in-flight retry of an ``event_id``-less
    payload land a second time.  Customer-level events get their own
    ``customer:`` namespace so the two can never collide.
    """
    if event.interaction_id is not None:
        key = f"{event.interaction_id}|{event.outcome_type}|{event.occurred_at or ''}"
    else:
        key = (
            f"customer:{event.customer_id}|{event.outcome_type}|"
            f"{event.occurred_at or ''}"
        )
    return "auto:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/outcomes", response_model=OutcomeAck, status_code=202)
async def ingest_outcome(
    request: Request,
    payload: OutcomeEvent,
    signature: Optional[str] = Header(None, alias="X-Linda-Signature"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    body = await request.body()
    if not _verify_hmac(tenant.outcomes_hmac_secret, body, signature):
        await _deadletter(
            db, tenant.id, "hmac_signature_invalid", payload, dict(request.headers)
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

    ack = await _apply_events(
        db, tenant.id, [payload], dict(request.headers), strict_customer=True
    )
    await db.commit()
    return ack


@router.post("/outcomes/batch", response_model=OutcomeAck, status_code=202)
async def ingest_outcomes_batch(
    request: Request,
    payload: OutcomeBatch,
    signature: Optional[str] = Header(None, alias="X-Linda-Signature"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    body = await request.body()
    if not _verify_hmac(tenant.outcomes_hmac_secret, body, signature):
        await _deadletter(
            db, tenant.id, "hmac_signature_invalid", payload, dict(request.headers)
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

    ack = await _apply_events(db, tenant.id, payload.events, dict(request.headers))
    await db.commit()
    return ack


@router.get("/outcomes/{interaction_id}")
async def get_outcomes(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    stmt = select(InteractionFeatures).where(
        InteractionFeatures.interaction_id == interaction_id,
        InteractionFeatures.tenant_id == tenant.id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Interaction features not found")
    return row.proxy_outcomes or {}


@router.get("/outcomes/dead-letter/recent")
async def dead_letter_recent(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> List[Dict[str, Any]]:
    """Admin-facing tail of the dead-letter log for this tenant."""
    from sqlalchemy import desc

    stmt = (
        select(DroppedOutcomeEvent)
        .where(DroppedOutcomeEvent.tenant_id == tenant.id)
        .order_by(desc(DroppedOutcomeEvent.received_at))
        .limit(max(1, min(limit, 500)))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "reason": r.reason,
            "payload": r.payload or {},
            "received_at": r.received_at.isoformat(),
        }
        for r in rows
    ]
