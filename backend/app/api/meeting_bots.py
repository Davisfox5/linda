"""Meeting-bot connector API (Enterprise ``meeting_assist`` feature).

LINDA dispatches a Recall.ai-compatible vendor bot to a Zoom/Meet/Teams
meeting URL; the vendor streams the meeting's audio back to LINDA's own
WebSocket ingress and we pipe it into the existing Deepgram
live-transcription pipeline — the same one telephony/AudioHook feed
(``backend.app.api.websocket`` / ``backend.app.api.audiohook``).

Endpoints
---------

* ``POST /meeting-bots`` — dispatch a bot. Human callers pass through
  ``require_scope`` (it no-ops for session/Clerk principals); API-key
  callers need the ``live:write`` scope. Every caller needs the tenant
  to carry the Enterprise ``meeting_assist`` feature flag.
* ``GET /meeting-bots`` / ``GET /meeting-bots/{job_id}`` — list/status,
  tenant-scoped via RLS as usual.
* ``DELETE /meeting-bots/{job_id}`` — ask the vendor to leave the call
  and finalize the linked live session.
* ``POST /meeting-bots/webhook`` — vendor bot-status webhook. Shared
  secret, NOT scope/feature gated (the vendor isn't a LINDA principal).
* ``WS /ws/meeting-bots/{bot_id}`` — vendor real-time audio ingress.
  Token-authenticated (query param minted at dispatch time), NOT
  behind ``require_feature``/``require_scope`` — the vendor connects
  anonymously and proves itself with the token.

See ``backend.app.services.meeting_bots`` for the vendor client, the
wire-format decision (Recall's ``audio_mixed_raw.data`` JSON envelope,
16 kHz mono S16LE PCM), and why tenant context for the last two routes
comes exclusively from the ``meetingbot:{key}`` Redis mapping rather
than a DB query.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal, require_scope
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import LiveSession, MeetingBotJob, Tenant
from backend.app.plans import require_feature
from backend.app.services.meeting_bots import (
    MeetingBotError,
    create_bot,
    forget_meetingbot,
    resolve_meetingbot,
    stop_bot,
)
from backend.app.tenant_ctx import bind_tenant_async, reset_current_tenant, set_current_tenant

logger = logging.getLogger(__name__)
router = APIRouter()

_TERMINAL_STATUSES = ("done", "failed")

# Same TTL the live websocket handlers use for their Redis buffer keys
# (see backend.app.api.websocket._LIVE_BUFFER_TTL_SECONDS) — kept as a
# local constant rather than an import so this module doesn't reach
# into websocket.py's private state, only its finalization helper.
_LIVE_BUFFER_TTL_SECONDS = 24 * 60 * 60


# ── Schemas ──────────────────────────────────────────────────────────


class CreateMeetingBotRequest(BaseModel):
    meeting_url: str = Field(..., min_length=8, max_length=2048)


class MeetingBotJobOut(BaseModel):
    id: uuid.UUID
    session_id: Optional[uuid.UUID]
    status: str
    platform: Optional[str]
    bot_id: Optional[str]
    meeting_url: str
    created_at: datetime
    ended_at: Optional[datetime]
    last_error: Optional[str]
    # Attach points for consumers, same shape as GET /live-sessions
    # (paths, not absolute URLs — callers know their own app host).
    monitor_ws_path: Optional[str] = None
    embed_path: Optional[str] = None


def _serialize(job: MeetingBotJob) -> MeetingBotJobOut:
    return MeetingBotJobOut(
        id=job.id,
        session_id=job.live_session_id,
        status=job.status,
        platform=job.platform,
        bot_id=job.bot_id,
        meeting_url=job.meeting_url,
        created_at=job.created_at,
        ended_at=job.ended_at,
        last_error=job.last_error,
        monitor_ws_path=(
            f"/ws/monitor/{job.live_session_id}" if job.live_session_id else None
        ),
        embed_path=(
            f"/embed/live/{job.live_session_id}" if job.live_session_id else None
        ),
    )


async def _finalize_session(session_id: str) -> None:
    """Finalize a meeting-bot's live session via the SAME code path the
    telephony ingresses use on clean hangup. Imported, not duplicated —
    see ``backend.app.api.telephony._finalize_or_defer`` for the sibling
    call site."""
    import redis.asyncio as aioredis

    from backend.app.api.websocket import _dispatch_batch_analysis

    settings = get_settings()
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await _dispatch_batch_analysis(redis, session_id)
    except Exception:
        logger.exception("meeting-bots: finalize failed for session %s", session_id)
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


# ── REST: dispatch / list / status / stop ───────────────────────────


@router.post(
    "/meeting-bots",
    response_model=MeetingBotJobOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("live:write"))],
)
async def dispatch_meeting_bot(
    body: CreateMeetingBotRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(require_feature("meeting_assist")),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> MeetingBotJobOut:
    """Dispatch a vendor bot to a Zoom/Meet/Teams meeting URL."""
    try:
        result = await create_bot(
            db,
            tenant=tenant,
            meeting_url=body.meeting_url,
            requested_by_user_id=principal.user_id,
            request_base_url=str(request.base_url),
        )
    except MeetingBotError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _serialize(result.job)


@router.get(
    "/meeting-bots",
    response_model=List[MeetingBotJobOut],
    dependencies=[Depends(require_scope("live:read"))],
)
async def list_meeting_bots(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(require_feature("meeting_assist")),
) -> List[MeetingBotJobOut]:
    stmt = (
        select(MeetingBotJob)
        .order_by(MeetingBotJob.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_serialize(r) for r in rows]


@router.get(
    "/meeting-bots/{job_id}",
    response_model=MeetingBotJobOut,
    dependencies=[Depends(require_scope("live:read"))],
)
async def get_meeting_bot(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(require_feature("meeting_assist")),
) -> MeetingBotJobOut:
    job = await db.get(MeetingBotJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="meeting bot job not found")
    return _serialize(job)


@router.delete(
    "/meeting-bots/{job_id}",
    response_model=MeetingBotJobOut,
    dependencies=[Depends(require_scope("live:write"))],
)
async def stop_meeting_bot(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(require_feature("meeting_assist")),
) -> MeetingBotJobOut:
    job = await db.get(MeetingBotJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="meeting bot job not found")
    if job.status in _TERMINAL_STATUSES:
        return _serialize(job)

    try:
        await stop_bot(job)
    except MeetingBotError:
        # Best-effort — still finalize our side so the operator isn't
        # stuck with a job that looks perpetually in-flight just because
        # the vendor call failed.
        logger.exception("meeting-bots: vendor stop_bot failed for job %s", job_id)

    session_id = job.live_session_id
    job.status = "done"
    job.ended_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(job)

    if session_id is not None:
        await _finalize_session(str(session_id))
    if job.bot_id:
        await forget_meetingbot(job.bot_id)
    await forget_meetingbot(str(job.id))

    return _serialize(job)


# ── Vendor status webhook ────────────────────────────────────────────


def _verify_webhook_secret(request: Request) -> bool:
    settings = get_settings()
    expected = settings.MEETING_BOT_WEBHOOK_SECRET
    if not expected:
        # Fail closed — an unconfigured secret must not be treated as
        # "auth disabled". Set MEETING_BOT_WEBHOOK_SECRET before
        # onboarding any tenant to meeting_assist.
        return False
    provided = request.headers.get("x-meeting-bot-secret", "")
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def _normalize_vendor_status(code: str) -> str:
    """Map a Recall-style ``status.code`` string onto our 4-state
    machine (``joining | in_call | done | failed``). Unknown codes fall
    back to ``in_call`` — the safest "still happening" assumption: a
    misclassified terminal state would leave a job stuck forever, while
    a misclassified in-progress state just delays finalization until
    the next event."""
    code = (code or "").lower()
    if any(tok in code for tok in ("fatal", "error", "fail")):
        return "failed"
    if any(tok in code for tok in ("done", "ended", "leave")):
        return "done"
    if "joining" in code:
        return "joining"
    return "in_call"


@router.post("/meeting-bots/webhook")
async def meeting_bot_webhook(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """Vendor bot-status webhook (Recall-compatible ``bot.status_change``
    shape)::

        {"event": "bot.status_change",
         "bot": {"id": "<vendor bot id>"},
         "status": {"code": "in_call_recording", "message": "..."}}

    Tenant/session identity is NEVER taken from the payload — only the
    ``meetingbot:{bot_id}`` Redis mapping written at dispatch time is
    trusted (see ``services.meeting_bots`` module docstring for why a
    DB fallback keyed on the vendor id can't work under RLS).
    """
    if not _verify_webhook_secret(request):
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    vendor_bot_id = str((body.get("bot") or {}).get("id") or body.get("bot_id") or "")
    if not vendor_bot_id:
        raise HTTPException(status_code=400, detail="missing bot id")

    ctx = await resolve_meetingbot(vendor_bot_id)
    if ctx is None:
        logger.warning("meeting-bots webhook: unknown bot id %s", vendor_bot_id)
        raise HTTPException(status_code=404, detail="unknown bot")

    try:
        tenant_uuid = uuid.UUID(str(ctx.get("tenant_id") or ""))
        job_uuid = uuid.UUID(str(ctx.get("job_id") or ""))
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown bot")

    await bind_tenant_async(db, tenant_uuid)
    job = await db.get(MeetingBotJob, job_uuid)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown bot")

    status_obj = body.get("status") or {}
    status_code = str(status_obj.get("code") or "")
    new_status = _normalize_vendor_status(status_code)
    job.status = new_status
    if new_status == "failed":
        job.last_error = str(status_obj.get("message") or status_code)[:500]

    is_terminal = new_status in _TERMINAL_STATUSES
    session_id = job.live_session_id
    if is_terminal:
        job.ended_at = datetime.now(timezone.utc)

    await db.commit()

    if is_terminal:
        if session_id is not None:
            await _finalize_session(str(session_id))
        if job.bot_id:
            await forget_meetingbot(job.bot_id)
        await forget_meetingbot(str(job.id))

    return {"status": "ok", "job_id": str(job.id), "job_status": job.status}


# ── Vendor real-time audio ingress ──────────────────────────────────


def _extract_pcm(frame: Dict[str, Any]) -> Optional[bytes]:
    """Pull raw 16 kHz mono S16LE PCM bytes out of one received WebSocket
    frame. Primary format: Recall's ``audio_mixed_raw.data`` JSON
    envelope over a text frame. Fallback: a raw binary frame carrying
    the same PCM directly (some vendor configs stream binary)."""
    raw_bytes = frame.get("bytes")
    if raw_bytes:
        return raw_bytes
    raw_text = frame.get("text")
    if raw_text:
        try:
            msg = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        if msg.get("event") != "audio_mixed_raw.data":
            return None
        data = ((msg.get("data") or {}).get("data") or {})
        buffer_b64 = data.get("buffer")
        if not buffer_b64:
            return None
        try:
            return base64.b64decode(buffer_b64)
        except (ValueError, binascii.Error):
            return None
    return None


@router.websocket("/ws/meeting-bots/{bot_id}")
async def meeting_bot_audio_ingress(websocket: WebSocket, bot_id: str) -> None:
    """Vendor real-time audio ingress for a dispatched meeting bot.

    Auth: the ``token`` query param must match the token minted for
    this bot at dispatch time (stored in the ``meetingbot:{bot_id}``
    Redis mapping) — never derived from anything the client sends.
    """
    token = websocket.query_params.get("token") if websocket.query_params else None
    ctx = await resolve_meetingbot(bot_id)
    if (
        ctx is None
        or not token
        or not hmac.compare_digest(str(ctx.get("token") or ""), token)
    ):
        await websocket.close(code=1008)
        return

    tenant_id_raw = ctx.get("tenant_id")
    session_id = ctx.get("session_id")
    if not tenant_id_raw or not session_id:
        await websocket.close(code=1008)
        return

    try:
        tenant_uuid = uuid.UUID(str(tenant_id_raw))
    except ValueError:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    settings = get_settings()
    tctx_token = set_current_tenant(tenant_uuid)
    redis = None
    dg_connection = None
    try:
        try:
            from deepgram import DeepgramClient
        except Exception:
            logger.exception("meeting-bots: deepgram-sdk missing; closing ingress")
            await websocket.close(code=1011)
            return

        import redis.asyncio as aioredis

        redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

        dg_client = DeepgramClient(settings.DEEPGRAM_API_KEY)
        dg_connection = dg_client.listen.live.v("1")

        async def on_transcript(_self: Any, result: Any, **kwargs: Any) -> None:
            alt = result.channel.alternatives[0] if result.channel.alternatives else None
            if alt is None or not alt.transcript:
                return
            text = alt.transcript
            speaker = alt.words[0].speaker if alt.words else None
            ts = time.time()
            payload = {
                "type": "final" if result.is_final else "partial",
                "text": text,
                "speaker": speaker,
                "timestamp": ts,
            }
            if redis is not None:
                await redis.publish(f"live:{session_id}:events", json.dumps(payload))
            if result.is_final and redis is not None:
                segment = json.dumps({"text": text, "speaker": speaker, "timestamp": ts})
                pipe = redis.pipeline(transaction=False)
                pipe.rpush(f"live:{session_id}:buffer", segment)
                pipe.expire(f"live:{session_id}:buffer", _LIVE_BUFFER_TTL_SECONDS)
                await pipe.execute()

        async def on_error(_self: Any, error: Any, **kwargs: Any) -> None:
            logger.error("meeting-bots: Deepgram error for session %s: %s", session_id, error)

        dg_connection.on("Results", on_transcript)
        dg_connection.on("Error", on_error)

        try:
            await dg_connection.start(
                {
                    "model": "nova-3",
                    "encoding": "linear16",
                    "sample_rate": 16000,
                    "channels": 1,
                    "interim_results": True,
                    "diarize": True,
                }
            )
        except Exception:
            logger.exception(
                "meeting-bots: failed to start Deepgram for session %s", session_id
            )
            await websocket.close(code=1011)
            return

        while True:
            data = await websocket.receive()
            pcm = _extract_pcm(data)
            if pcm:
                await dg_connection.send(pcm)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("meeting-bots: ingress crashed for bot %s", bot_id)
    finally:
        if dg_connection is not None:
            try:
                await dg_connection.finish()
            except Exception:
                pass
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass
        reset_current_tenant(tctx_token)


__all__ = ["router"]
