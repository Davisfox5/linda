"""Live-session discovery API (Enterprise).

Closes the loop for programmatic live-transcription consumers: a
dialer/CRM that only holds its own call id (Twilio CallSid, Telnyx call
control id, SIPREC Call-ID, meeting-bot id) can resolve the LINDA
session, then mint a monitor ticket (``POST /ws/tickets``) and attach —
or screen-pop the first-party view at ``/live/{session_id}`` / embed
``/embed/live/{session_id}``.

Auth: humans (session/clerk) pass with plain auth — this is read-only
dashboard-equivalent data. API keys additionally need the Enterprise
``live_transcription_api`` entitlement (applied by the Stripe webhook
via ``plans.apply_tier``) and the ``live:read`` scope, matching the
ticket endpoint's gate.

Rows are tenant-scoped by RLS as usual; ``ix_live_sessions_tenant_status``
serves the active listing, ``ix_live_sessions_external_call_id`` the
lookup.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal
from backend.app.db import get_db
from backend.app.models import LiveSession
from backend.app.plans import limits_for
from backend.app.services.entitlements import tenant_is_comped

router = APIRouter()


# Ingress paths are inconsistent about the in-flight status label
# (browser/telephony use "active", SIPREC uses "live").
_LIVE_STATUSES = ("active", "live")


class LiveSessionOut(BaseModel):
    id: uuid.UUID
    source: Optional[str]
    status: str
    external_call_id: Optional[str]
    agent_id: Optional[uuid.UUID]
    interaction_id: Optional[uuid.UUID]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    # Attach points for consumers (paths, not absolute URLs — callers
    # know their own API/app hosts).
    monitor_ws_path: str
    embed_path: str


def _serialize(row: LiveSession) -> LiveSessionOut:
    return LiveSessionOut(
        id=row.id,
        source=row.source,
        status=row.status,
        external_call_id=getattr(row, "external_call_id", None),
        agent_id=row.agent_id,
        interaction_id=row.interaction_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        monitor_ws_path="/ws/monitor/{0}".format(row.id),
        embed_path="/embed/live/{0}".format(row.id),
    )


async def _require_live_read(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> AuthPrincipal:
    """Gate API-key callers on the Enterprise entitlement + live:read."""
    if principal.source == "api_key":
        tenant = principal.tenant
        if not tenant_is_comped(tenant) and not bool(
            limits_for(tenant).features.get("live_transcription_api", False)
        ):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    "Live transcription API access requires the Enterprise "
                    "plan ('live_transcription_api')."
                ),
            )
        if not principal.has_scope("live:read"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="missing scope: live:read",
            )
    return principal


@router.get("/live-sessions", response_model=List[LiveSessionOut])
async def list_live_sessions(
    state: Literal["active", "all"] = Query(default="active"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(_require_live_read),
):
    """List the tenant's live sessions (in-flight by default)."""
    stmt = select(LiveSession).order_by(LiveSession.started_at.desc()).limit(limit)
    if state == "active":
        stmt = stmt.where(LiveSession.status.in_(_LIVE_STATUSES))
    rows = (await db.execute(stmt)).scalars().all()
    return [_serialize(r) for r in rows]


@router.get("/live-sessions/lookup", response_model=LiveSessionOut)
async def lookup_live_session(
    external_call_id: str = Query(..., min_length=1, max_length=256),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(_require_live_read),
):
    """Resolve a provider-side call id to the LINDA live session.

    Most-recent match wins — a redialed CallSid maps to the newest
    session carrying it.
    """
    stmt = (
        select(LiveSession)
        .where(LiveSession.external_call_id == external_call_id)
        .order_by(LiveSession.started_at.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="no live session for that external_call_id",
        )
    return _serialize(row)


@router.get("/live-sessions/{session_id}", response_model=LiveSessionOut)
async def get_live_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(_require_live_read),
):
    row = await db.get(LiveSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _serialize(row)
