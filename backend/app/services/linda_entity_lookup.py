"""Entity lookup for Ask LINDA — name/email/domain → the ids tools need.

Every write tool in :mod:`backend.app.services.linda_agent` is keyed on an
id (``interaction_id``, ``prospect_id``, ``campaign_id``), but until this
module existed the read tools returned exactly one kind of id
(``interaction_id``, plus ``campaign_id`` since the campaign work). There
was no way to get from "Acme" or "dana@acme.com" to a customer, prospect,
contact, or teammate — which made ``propose_queue_bump_email`` (it wants a
``Customer`` UUID) unreachable in practice.

This is deliberately **deterministic SQL, no LLM**: it is a lookup, not an
inference. The fuzzy interaction→entity resolution that *does* use a model
lives in :mod:`backend.app.services.entity_resolution` and runs in the
analysis pipeline; this module only searches rows that already exist.

Tenant scoping is explicit on every query (belt-and-braces with the RLS
backstop), matching the other Linda tool executors.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Contact, Customer, Tenant, User

# Match kinds, strongest first. ``confidence`` is a fixed score per match
# reason rather than a learned one — the point is a stable, explainable
# ordering the model can reason about, not a calibrated probability.
_EXACT_EMAIL = ("exact_email", 1.0)
_EXACT_NAME = ("exact_name", 0.9)
_DOMAIN = ("domain", 0.8)
_PARTIAL_NAME = ("partial_name", 0.6)

VALID_KINDS = ("customer", "contact", "user")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def _domain_of(value: str) -> Optional[str]:
    """The domain part of an email, or a bare domain typed on its own."""
    if "@" in value:
        tail = value.rsplit("@", 1)[-1].strip()
        return tail or None
    if "." in value and " " not in value:
        return value
    return None


def clamp_limit(value: Any, default: int = 8, lo: int = 1, hi: int = 25) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(lo, min(hi, limit))


def normalize_kinds(raw: Any) -> List[str]:
    """Coerce the model-supplied ``kinds`` argument to a valid subset.

    Anything unrecognized (or omitted) means "search everything" — a bad
    filter should widen the search, never silently return zero rows.
    """
    if not raw:
        return list(VALID_KINDS)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return list(VALID_KINDS)
    kinds = [str(k).strip().lower() for k in raw]
    kept = [k for k in VALID_KINDS if k in kinds]
    return kept or list(VALID_KINDS)


async def resolve_entity(
    db: AsyncSession,
    tenant: Tenant,
    query: str,
    kinds: Optional[Sequence[str]] = None,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Find tenant entities matching a free-form name / email / domain.

    Returns candidates ordered by descending confidence then name, each
    carrying the id its kind is addressed by plus enough context for the
    model to pick the right one without a second round-trip (a customer's
    pipeline/DNC state, a contact's parent customer, a user's role).
    """
    q = (query or "").strip()
    if not q:
        return []

    wanted = normalize_kinds(kinds)
    limit = clamp_limit(limit)
    like = "%%%s%%" % q
    domain = _domain_of(q)

    rows: List[Dict[str, Any]] = []
    if "customer" in wanted:
        rows.extend(await _customers(db, tenant, q, like, domain, limit))
    if "contact" in wanted:
        rows.extend(await _contacts(db, tenant, q, like, domain, limit))
    if "user" in wanted:
        rows.extend(await _users(db, tenant, q, like, limit))

    rows.sort(key=lambda r: (-r["confidence"], (r.get("display_name") or "").lower()))
    return rows[:limit]


async def _customers(
    db: AsyncSession,
    tenant: Tenant,
    q: str,
    like: str,
    domain: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    conditions = [Customer.name.ilike(like)]
    if domain:
        conditions.append(Customer.domain.ilike(domain))
    stmt = (
        select(Customer)
        .where(Customer.tenant_id == tenant.id, or_(*conditions))
        .order_by(Customer.name)
        .limit(limit * 2)
    )
    out: List[Dict[str, Any]] = []
    for c in (await db.execute(stmt)).scalars().all():
        name = c.name or ""
        if domain and (c.domain or "").lower() == domain.lower():
            match, confidence = _DOMAIN
        elif name.lower() == q.lower():
            match, confidence = _EXACT_NAME
        else:
            match, confidence = _PARTIAL_NAME
        out.append(
            {
                "kind": "customer",
                "id": str(c.id),
                "display_name": name,
                "domain": c.domain,
                "match": match,
                "confidence": confidence,
                # Outreach state — the model needs this before proposing a
                # bump: the confirm endpoint 409s on a do-not-contact
                # prospect, so surfacing it here avoids a dead proposal.
                "is_prospect": c.pipeline_status is not None,
                "pipeline_status": c.pipeline_status,
                "do_not_contact": bool(c.do_not_contact),
                "health_score": c.health_score,
                "renewal_date": c.renewal_date.isoformat() if c.renewal_date else None,
            }
        )
    return out


async def _contacts(
    db: AsyncSession,
    tenant: Tenant,
    q: str,
    like: str,
    domain: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    conditions = [Contact.name.ilike(like), Contact.email.ilike(like)]
    if domain:
        conditions.append(Contact.email.ilike("%%@%s" % domain))
    stmt = (
        select(Contact)
        .where(Contact.tenant_id == tenant.id, or_(*conditions))
        .order_by(Contact.name)
        .limit(limit * 2)
    )
    contacts = (await db.execute(stmt)).scalars().all()

    # One batched lookup for the parent customers rather than N gets —
    # the model almost always wants "which account is this person at".
    customer_ids = {c.customer_id for c in contacts if c.customer_id is not None}
    names: Dict[Any, str] = {}
    if customer_ids:
        name_rows = (
            await db.execute(
                select(Customer.id, Customer.name).where(
                    Customer.tenant_id == tenant.id, Customer.id.in_(customer_ids)
                )
            )
        ).all()
        names = {row[0]: row[1] for row in name_rows}

    out: List[Dict[str, Any]] = []
    for c in contacts:
        email = (c.email or "").lower()
        name = c.name or ""
        if email and email == q.lower():
            match, confidence = _EXACT_EMAIL
        elif name.lower() == q.lower():
            match, confidence = _EXACT_NAME
        elif domain and email.endswith("@%s" % domain.lower()):
            match, confidence = _DOMAIN
        else:
            match, confidence = _PARTIAL_NAME
        out.append(
            {
                "kind": "contact",
                "id": str(c.id),
                "display_name": name or c.email or "",
                "email": c.email,
                "role": c.role,
                "match": match,
                "confidence": confidence,
                "customer_id": str(c.customer_id) if c.customer_id else None,
                "customer_name": names.get(c.customer_id),
                "interaction_count": c.interaction_count,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
            }
        )
    return out


async def _users(
    db: AsyncSession, tenant: Tenant, q: str, like: str, limit: int
) -> List[Dict[str, Any]]:
    stmt = (
        select(User)
        .where(
            User.tenant_id == tenant.id,
            or_(User.name.ilike(like), User.email.ilike(like)),
        )
        .order_by(User.email)
        .limit(limit * 2)
    )
    out: List[Dict[str, Any]] = []
    for u in (await db.execute(stmt)).scalars().all():
        email = (u.email or "").lower()
        name = u.name or ""
        if email and email == q.lower():
            match, confidence = _EXACT_EMAIL
        elif name.lower() == q.lower():
            match, confidence = _EXACT_NAME
        else:
            match, confidence = _PARTIAL_NAME
        out.append(
            {
                "kind": "user",
                "id": str(u.id),
                "display_name": name or u.email,
                # assignee_email is what the action-item/plan tools take,
                # so return it explicitly rather than making the model
                # reconstruct it from display_name.
                "email": u.email,
                "role": u.role,
                "is_active": bool(u.is_active),
                "match": match,
                "confidence": confidence,
            }
        )
    return out
