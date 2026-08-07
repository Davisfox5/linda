"""Outbound webhook emission for live-session lifecycle.

One tiny seam shared by every live ingress (browser WS, Twilio /
SignalWire / Telnyx media streams, SIPREC, AudioHook, meeting bots) so
API consumers can discover sessions the moment they start — the
``live_session.started`` payload carries everything needed to mint a
monitor ticket and attach (session_id, source, external_call_id).

Opens its own DB session and binds the tenant GUC (mirrors the
brief-alert fanout in ``backend/app/api/websocket.py``): callers sit on
unauthenticated webhook paths or WS teardown, so no request-scoped
session/tenant binding can be assumed. Best-effort by design — a
failed emission logs and never breaks call handling.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


async def emit_live_session_event(
    tenant_id: Union[str, uuid.UUID],
    session_id: str,
    event: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit ``live_session.started`` / ``live_session.completed``.

    ``extra`` is merged into the payload (source, external_call_id,
    interaction_id, …). Never raises.
    """
    try:
        from backend.app.db import async_session
        from backend.app.services.webhook_dispatcher import emit_event
        from backend.app.tenant_ctx import tenant_context_async

        payload: Dict[str, Any] = {"session_id": str(session_id)}
        for k, v in (extra or {}).items():
            payload[k] = str(v) if isinstance(v, uuid.UUID) else v

        async with async_session() as db:
            async with tenant_context_async(tenant_id, db):
                await emit_event(db, tenant_id, event, payload)
                await db.commit()
    except Exception:
        logger.debug(
            "live-session webhook emission failed (event=%s session=%s)",
            event,
            session_id,
            exc_info=True,
        )
