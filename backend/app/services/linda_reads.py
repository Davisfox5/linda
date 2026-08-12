"""Tier-1 read executors for Ask LINDA.

These expose surfaces the chat's system prompt already advertises but the
tool registry couldn't reach: a customer's current state, the tenant's own
knowledge base, the orchestrator's profile trees, and period metrics.

Two rules shape how they're built:

* **Reuse the existing implementation, don't restate it.** ``get_profile``
  runs the *same* RBAC gates as ``api/profiles.py`` and ``get_team_metrics``
  delegates to the analytics dashboard endpoint, so a chat answer and the
  SPA can never disagree. Duplicating either would create exactly the
  drift that made ``propose_crm_update`` confirm to a no-op.
* **Report stored values honestly.** ``get_customer_360`` reads the health
  score the CS pipeline persisted (with no claim of freshness) rather than
  recomputing it — the compute path is sync/Celery-shaped and the chat
  request path is async.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    AgentProfile,
    BusinessProfile,
    ClientProfile,
    Contact,
    Customer,
    CustomerCommitment,
    CustomerConcern,
    Interaction,
    ManagerProfile,
    Tenant,
    User,
)

logger = logging.getLogger(__name__)

PROFILE_KINDS = ("client", "agent", "manager", "business")

_PROFILE_MODELS = {
    "client": (ClientProfile, "contact_id"),
    "agent": (AgentProfile, "agent_id"),
    "manager": (ManagerProfile, "manager_id"),
    # NB: business profiles key on ``business_tenant_id``, not ``tenant_id``
    # (both columns exist) — same column api/profiles.py:303 selects on.
    "business": (BusinessProfile, "business_tenant_id"),
}


def _clamp(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return max(lo, min(hi, out))


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


# ── Customer 360 ───────────────────────────────────────────────────────────


async def customer_360(
    db: AsyncSession, tenant: Tenant, customer_id: Any, interaction_limit: int = 5
) -> Dict[str, Any]:
    """Everything worth knowing about one account, in one call.

    Answers "what's going on with Acme?" — which chat could previously only
    approximate by full-text searching transcripts.
    """
    customer_uuid = _parse_uuid(customer_id)
    if customer_uuid is None:
        return {"error": "invalid customer_id"}

    customer = (
        await db.execute(
            select(Customer).where(
                Customer.id == customer_uuid, Customer.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        return {"error": "customer not found"}

    limit = _clamp(interaction_limit, default=5, lo=1, hi=15)

    concerns = (
        await db.execute(
            select(CustomerConcern)
            .where(
                CustomerConcern.tenant_id == tenant.id,
                CustomerConcern.customer_id == customer_uuid,
                CustomerConcern.status == "active",
            )
            .order_by(CustomerConcern.last_seen_at.desc())
            .limit(10)
        )
    ).scalars().all()

    commitments = (
        await db.execute(
            select(CustomerCommitment)
            .where(
                CustomerCommitment.tenant_id == tenant.id,
                CustomerCommitment.customer_id == customer_uuid,
                CustomerCommitment.status == "open",
            )
            .order_by(CustomerCommitment.due_date.asc().nulls_last())
            .limit(10)
        )
    ).scalars().all()

    interactions = (
        await db.execute(
            select(Interaction)
            .where(
                Interaction.tenant_id == tenant.id,
                Interaction.customer_id == customer_uuid,
            )
            .order_by(Interaction.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    contacts = (
        await db.execute(
            select(Contact)
            .where(
                Contact.tenant_id == tenant.id,
                Contact.customer_id == customer_uuid,
            )
            .order_by(Contact.interaction_count.desc())
            .limit(10)
        )
    ).scalars().all()

    return {
        "customer": {
            "id": str(customer.id),
            "name": customer.name,
            "domain": customer.domain,
            "industry": customer.industry,
            # Persisted by the CS pipeline, not recomputed here — say so
            # rather than implying it's live.
            "health_score": customer.health_score,
            "health_score_note": "last computed by the CS health job, not live",
            "onboarding_status": customer.onboarding_status,
            "renewal_date": (
                customer.renewal_date.isoformat() if customer.renewal_date else None
            ),
            "pipeline_status": customer.pipeline_status,
            "do_not_contact": bool(customer.do_not_contact),
        },
        "open_concerns": [
            {
                "topic": c.topic,
                "description": c.description,
                "severity": c.severity,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "last_seen_interaction_id": (
                    str(c.last_seen_interaction_id)
                    if c.last_seen_interaction_id
                    else None
                ),
            }
            for c in concerns
        ],
        "open_commitments": [
            {
                "description": c.description,
                "quote": c.quote,
                "due_date": c.due_date.isoformat() if c.due_date else None,
                "source_interaction_id": (
                    str(c.source_interaction_id) if c.source_interaction_id else None
                ),
            }
            for c in commitments
        ],
        "contacts": [
            {
                "id": str(c.id),
                "name": c.name,
                "email": c.email,
                "role": c.role,
                "interaction_count": c.interaction_count,
            }
            for c in contacts
        ],
        "recent_interactions": [
            {
                "interaction_id": str(i.id),
                "title": i.title,
                "channel": i.channel,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "summary": (i.insights or {}).get("summary"),
                "sentiment_overall": (i.insights or {}).get("sentiment_overall"),
            }
            for i in interactions
        ],
    }


# ── Knowledge base ─────────────────────────────────────────────────────────


async def search_knowledge_base(
    db: AsyncSession, tenant: Tenant, query: str, limit: int = 5
) -> Dict[str, Any]:
    """Retrieve the tenant's own KB documents for a question.

    Without this the model answers "what's our refund policy" from its
    training data — the highest-risk hallucination surface in the product,
    because the answer sounds exactly as confident either way.
    """
    from backend.app.services import kb_document_retrieval

    q = (query or "").strip()
    if not q:
        return {"documents": [], "count": 0}

    k = _clamp(limit, default=5, lo=1, hi=10)
    try:
        hits = await kb_document_retrieval.retrieve(db, tenant.id, q, k=k)
    except Exception as exc:
        logger.exception("search_knowledge_base failed")
        return {"error": "knowledge-base search failed: %s" % exc}

    documents: List[Dict[str, Any]] = []
    for doc, score in hits:
        documents.append(
            {
                "document_id": str(doc.id),
                "title": doc.title,
                "score": round(float(score), 4),
                "excerpt": (doc.content or "")[:800],
                "source_type": doc.source_type,
                "source_url": doc.source_url,
                "last_synced_at": (
                    doc.last_synced_at.isoformat() if doc.last_synced_at else None
                ),
            }
        )
    return {"query": q, "count": len(documents), "documents": documents}


# ── Profiles (RBAC-gated) ──────────────────────────────────────────────────


def build_principal(tenant: Tenant, user: Optional[User]) -> Any:
    """An ``AuthPrincipal`` for the chat caller, mirroring ``auth.py``.

    Chat resolves its user best-effort (``api/chat.py::_resolve_current_user``)
    and gets ``None`` for tenant-API-key callers. That is NOT "unknown, deny":
    ``auth.py`` documents API keys as programmatic tenant-wide credentials and
    builds their principal with ``role="admin"``. Chat must match, or the same
    key that can read a profile straight off ``/api/v1/profiles/...`` would be
    refused when it asks Linda — friction without a security gain.
    """
    from backend.app.auth import AuthPrincipal, _resolve_effective_role

    if user is None:
        return AuthPrincipal(
            tenant=tenant, user=None, role="admin", source="api_key", scopes=["*"]
        )
    effective_role, is_previewing = _resolve_effective_role(user, tenant)
    return AuthPrincipal(
        tenant=tenant,
        user=user,
        role=effective_role,
        source="session",
        is_previewing=is_previewing,
    )


async def get_profile(
    db: AsyncSession,
    tenant: Tenant,
    user: Optional[User],
    kind: str,
    entity_id: Any = None,
) -> Dict[str, Any]:
    """The latest client / agent / manager / business profile.

    These are the four trees the nightly Opus orchestrator maintains — the
    product's own read on an account, a rep, a manager, and the business.

    Access is gated by the SAME functions ``api/profiles.py`` uses, imported
    rather than reimplemented: this is a role-scoped surface (unlike the
    tenant-scoped interaction reads), and a second copy of the rules would
    eventually disagree with the first.
    """
    from backend.app.api.profiles import (
        _authorize_agent_access,
        _authorize_business_access,
        _authorize_client_access,
        _authorize_manager_access,
        _project,
    )
    from fastapi import HTTPException

    kind = (kind or "").strip().lower()
    if kind not in _PROFILE_MODELS:
        return {"error": "unknown profile kind %r — use one of %s" % (kind, ", ".join(PROFILE_KINDS))}

    principal = build_principal(tenant, user)

    if kind == "business":
        target = tenant.id
    else:
        target = _parse_uuid(entity_id)
        if target is None:
            return {
                "error": "%s profiles need an entity_id (a %s id) — find one with resolve_entity"
                % (kind, "contact" if kind == "client" else "user")
            }

    try:
        if kind == "client":
            await _authorize_client_access(db, principal, target)
        elif kind == "agent":
            await _authorize_agent_access(db, principal, target)
        elif kind == "manager":
            await _authorize_manager_access(principal, target)
        else:
            await _authorize_business_access(principal)
    except HTTPException:
        # Deliberately the same phrasing whatever the reason: telling the
        # caller "that agent has no profile" vs "you may not see it" leaks
        # which entities exist.
        return {
            "error": "You don't have access to that profile.",
            "kind": kind,
        }

    model, fk = _PROFILE_MODELS[kind]
    row = (
        await db.execute(
            select(model)
            .where(getattr(model, fk) == target)
            .order_by(model.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or row.tenant_id != tenant.id:
        return {"error": "no %s profile has been built yet" % kind, "kind": kind}

    projected = _project(row, target, tenant)
    payload = projected.model_dump()
    payload["entity_id"] = str(payload["entity_id"])
    return {"kind": kind, "profile": payload}


# ── Team metrics ───────────────────────────────────────────────────────────


VALID_PERIODS = ("7d", "14d", "30d", "60d", "90d")


async def team_metrics(
    db: AsyncSession, tenant: Tenant, period: str = "30d"
) -> Dict[str, Any]:
    """Period rollup — volume, sentiment, QA, action-item and risk counts.

    Delegates to the analytics dashboard endpoint so chat and the SPA quote
    the same numbers from the same SQL. "How did the team do this week?"
    previously cost N searches and could exhaust the tool-cycle budget
    before answering.
    """
    from backend.app.api.analytics import dashboard

    period = (period or "30d").strip().lower()
    if period not in VALID_PERIODS:
        return {
            "error": "unknown period %r — use one of %s"
            % (period, ", ".join(VALID_PERIODS))
        }

    try:
        summary = await dashboard(period=period, db=db, tenant=tenant)
    except Exception as exc:
        logger.exception("team_metrics failed")
        return {"error": "metrics lookup failed: %s" % exc}

    payload = (
        summary.model_dump() if hasattr(summary, "model_dump") else dict(summary)
    )
    payload["period"] = period
    payload["note"] = (
        "prev_period_deltas are percent changes vs the immediately preceding "
        "window of the same length."
    )
    return payload
