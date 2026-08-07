"""Telephony ingress — live-stream transcription only.

LINDA does not originate calls, place them on hold, or transfer them.
Those controls live in the tenant's phone system (contact center, MiaRec,
Teams, MetaSwitch, etc.). What we expose here is narrow:

1. **Inbound voice webhooks** (Twilio / SignalWire / Telnyx) — return
   TwiML (or the provider equivalent) that bridges the live audio into
   our Media Streams WebSocket so we can transcribe + coach in real
   time.
2. **Media Streams WebSockets** — accept base64 μ-law frames from the
   provider, forward to Deepgram live, and dispatch batch analysis when
   the call ends. Audio bytes are never persisted to disk or object
   storage.
3. **Admin**: link the tenant's Twilio credentials (used for inbound
   signature verification).

Post-call recordings from tenant recording systems come in through
``POST /interactions/ingest-recording`` (see ``backend.app.api.interactions``),
not here.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import Response

from backend.app.auth import AuthPrincipal, get_current_principal, require_role
from backend.app.config import get_settings
from backend.app.db import async_session, get_db
from backend.app.models import Integration, LiveSession, Tenant
from backend.app.services.telephony.twilio import (
    build_voice_twiml,
    decode_media_payload,
    validate_twilio_signature,
)
from backend.app.services.token_crypto import decrypt_token
from backend.app.tenant_ctx import bind_tenant_async, resolve_tenant_via_async

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class TwilioCreds:
    """Resolved per-tenant Twilio credentials. ``source`` is ``"tenant"``
    when the Integration row provided them or ``"env"`` for the
    single-tenant dev fallback."""

    account_sid: str
    auth_token: str
    source: str


async def _twilio_creds(
    tenant_id: uuid.UUID, db: AsyncSession
) -> TwilioCreds:
    """Resolve Twilio credentials for a tenant. Used only for webhook
    signature verification — we no longer originate calls."""
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == "twilio")
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is not None:
        cfg = integ.provider_config or {}
        account_sid = str(cfg.get("account_sid") or "")
        auth_token = decrypt_token(integ.access_token) or ""
        if account_sid and auth_token:
            return TwilioCreds(
                account_sid=account_sid, auth_token=auth_token, source="tenant"
            )

    settings = get_settings()
    return TwilioCreds(
        account_sid=settings.TWILIO_ACCOUNT_SID or "",
        auth_token=settings.TWILIO_AUTH_TOKEN or "",
        source="env",
    )


# ── Inbound webhook (TwiML) ───────────────────────────────────────────


@router.post("/telephony/twilio/voice")
async def twilio_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Twilio hits this URL when a call rings. We return TwiML that
    bridges audio to our Media Streams WebSocket.

    Audio is transcribed in real time and discarded — recordings (if any)
    are produced by the tenant's own recording system and POSTed to
    ``/interactions/ingest-recording`` as a separate flow.
    """
    form = await request.form()
    form_dict: Dict[str, str] = {k: str(v) for k, v in form.multi_items()}

    # tenant_id is a query param here, known up front — bind before the
    # Integration read in _twilio_creds (bootstrap-readable) so the
    # LiveSession insert below runs with the GUC armed.
    await bind_tenant_async(db, tenant_id)

    creds = await _twilio_creds(tenant_id, db)
    if creds.auth_token:
        signature = request.headers.get("X-Twilio-Signature", "")
        request_url = str(request.url)
        if not validate_twilio_signature(
            auth_token=creds.auth_token,
            request_url=request_url,
            params=form_dict,
            signature_header=signature,
        ):
            logger.warning(
                "Twilio webhook signature mismatch (tenant=%s, creds_source=%s)",
                tenant_id,
                creds.source,
            )
            raise HTTPException(status_code=403, detail="Invalid signature")

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    session = LiveSession(
        tenant_id=tenant_id,
        agent_id=tenant_id,  # populated when an agent claims the session
        source="twilio",
        status="active",
        external_call_id=form_dict.get("CallSid") or None,
    )
    db.add(session)
    await db.flush()

    from backend.app.services.live_session_events import emit_live_session_event

    await emit_live_session_event(
        tenant_id,
        str(session.id),
        "live_session.started",
        {"source": "twilio", "external_call_id": session.external_call_id},
    )

    base = str(request.base_url).rstrip("/").replace("http://", "wss://").replace(
        "https://", "wss://"
    )
    stream_url = f"{base}/ws/telephony/twilio/{session.id}"

    greeting: Optional[str] = None
    if getattr(tenant, "pii_redaction_enabled", False):
        greeting = (
            "This call may be transcribed for quality assurance. "
            "Audio is not retained."
        )

    twiml = build_voice_twiml(
        session_id=str(session.id),
        stream_url=stream_url,
        greeting=greeting,
    )
    return Response(content=twiml, media_type="application/xml")


# ── Media Streams WebSocket ───────────────────────────────────────────


@router.websocket("/ws/telephony/twilio/{session_id}")
async def twilio_media_stream(websocket: WebSocket, session_id: str):
    """Receive Twilio Media Streams frames and forward audio to Deepgram.

    Twilio sends JSON text frames with these events:

    * ``connected`` — handshake, contains protocol version.
    * ``start`` — call has begun; ``start.mediaFormat`` confirms audio
      encoding (audio/x-mulaw, 8kHz).
    * ``media`` — a base64-encoded audio chunk in ``media.payload``.
    * ``mark`` — marker echo (for sync, not used here).
    * ``stop`` — call ended.

    We decode the payload on each ``media`` frame, push raw bytes into
    the Deepgram live connection, and throw them away — nothing ever
    reaches disk or S3.
    """
    await websocket.accept()

    settings = get_settings()
    try:
        from deepgram import DeepgramClient
    except Exception:  # pragma: no cover — library is required
        logger.exception("deepgram-sdk missing; closing Twilio WS")
        await websocket.close(code=1011)
        return

    dg_client = DeepgramClient(settings.DEEPGRAM_API_KEY)
    dg_connection = dg_client.listen.live.v("1")

    # Provider defaults to twilio — the SignalWire + Telnyx paths delegate
    # here and keep the same metric label because framing is identical.
    try:
        from backend.app.services.metrics import (
            LIVE_DEEPGRAM_WS_CONNECTS,
            LIVE_PARALINGUISTIC_SNAPSHOTS,
            LIVE_SESSIONS,
        )

        LIVE_SESSIONS.labels(provider="twilio").inc()
    except Exception:
        LIVE_DEEPGRAM_WS_CONNECTS = LIVE_PARALINGUISTIC_SNAPSHOTS = None  # type: ignore
        LIVE_SESSIONS = None  # type: ignore

    import asyncio
    import redis.asyncio as aioredis

    from backend.app.services.telephony import live_session_resume as resume

    # Grace-period re-attach: register this connection and learn where
    # the call's timeline already stands (0.0 for a fresh call). If a
    # prior connection dropped within the grace window, its deferred
    # finalizer sees the new generation and stands down.
    session_redis = None
    attempt = None
    try:
        session_redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        attempt = await resume.begin_connection(session_redis, session_id)
        if attempt.resumed:
            logger.info(
                "Live session %s re-attached (gen=%d) at %.1fs — resuming timeline",
                session_id,
                attempt.generation,
                attempt.resume_offset_sec,
            )
    except Exception:
        logger.debug("resume state unavailable for %s", session_id, exc_info=True)
    resume_offset = attempt.resume_offset_sec if attempt is not None else 0.0

    # Per-tenant live paralinguistic surface (opt-in). The window runs
    # on CPU in the worker thread-pool so we don't block Deepgram I/O.
    live_para_window = None
    try:
        async with async_session() as db:
            # Only session_id is known here (no tenant on the path/query) —
            # resolve it via the SECURITY DEFINER function first (it works
            # RLS-blind and ends the transaction with rollback), then bind
            # before the LiveSession/Tenant loads that follow.
            resolved_tenant_id = await resolve_tenant_via_async(
                db, "live_sessions", uuid.UUID(session_id)
            )
            if resolved_tenant_id is None:
                if LIVE_SESSIONS is not None:
                    LIVE_SESSIONS.labels(provider="twilio").dec()
                await websocket.close(code=1008)
                return
            await bind_tenant_async(db, resolved_tenant_id)

            sess = await db.get(LiveSession, uuid.UUID(session_id))
            if sess is not None:
                sess.status = "live"
                tenant = await db.get(Tenant, sess.tenant_id)
                feats = (getattr(tenant, "features_enabled", None) or {})
                if feats.get("paralinguistic_live"):
                    from backend.app.services.paralinguistics_live import (
                        LiveParalinguisticWindow,
                    )

                    live_para_window = LiveParalinguisticWindow(
                        start_offset=resume_offset
                    )
    except Exception:
        logger.debug("Couldn't update LiveSession.status", exc_info=True)

    # Deepgram live → diarization timeline → paralinguistic window.
    # We register the event handler before start() so the first
    # Results frame doesn't slip through. The SDK invokes handlers on
    # its own thread; the window is single-writer (event loop only),
    # so the handler marshals turns onto this loop instead of touching
    # the window from the SDK thread. On a re-attach, Deepgram's word
    # offsets restart at zero for the new connection — shift them by
    # the resume offset so they line up with the window's timeline.
    if live_para_window is not None:
        _attach_deepgram_diarization(
            dg_connection,
            live_para_window,
            loop=asyncio.get_running_loop(),
            call_start_offset=resume_offset,
        )

    try:
        await dg_connection.start(
            {
                "model": "nova-3",
                "encoding": "mulaw",
                "sample_rate": 8000,
                "channels": 1,
                "interim_results": True,
                "diarize": True,
            }
        )
        if LIVE_DEEPGRAM_WS_CONNECTS is not None:
            LIVE_DEEPGRAM_WS_CONNECTS.labels(status="success").inc()
    except Exception:
        logger.exception("Failed to start Deepgram connection for session %s", session_id)
        if LIVE_DEEPGRAM_WS_CONNECTS is not None:
            LIVE_DEEPGRAM_WS_CONNECTS.labels(status="failed").inc()
        if LIVE_SESSIONS is not None:
            LIVE_SESSIONS.labels(provider="twilio").dec()
        await websocket.close(code=1011)
        return

    from backend.app.services.telephony.media_stream_pump import MediaStreamPump

    # One Redis connection per session — shared by the snapshot
    # publisher instead of a fresh connection every 3 s.
    publish = None
    if live_para_window is not None and session_redis is not None:
        publish = _make_paralinguistic_publisher(session_redis, session_id)

    # The receive loop below only decodes and enqueues; the pump's
    # consumer task forwards audio to Deepgram and runs the (bounded,
    # deadline-guarded) paralinguistic snapshots. A slow Praat can no
    # longer stall ingest — overload drops the oldest frames and shows
    # up in linda_live_media_frames_dropped_total.
    pump = MediaStreamPump(
        send_audio=dg_connection.send,
        window=live_para_window,
        publish=publish,
        provider="twilio",
        initial_audio_seconds=resume_offset,
    )
    pump_task = asyncio.get_event_loop().create_task(pump.run())

    clean_stop = False
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = frame.get("event")
            if event == "media":
                media = frame.get("media") or {}
                payload = media.get("payload") or ""
                audio = decode_media_payload(payload)
                if audio:
                    # Whole-window aggregate — we intentionally do not
                    # trust the provider-reported ``track`` as a proxy
                    # for agent/customer; diarization owns that.
                    pump.offer(audio)
            elif event == "stop":
                clean_stop = True
                break
            elif event == "start":
                logger.info(
                    "Twilio stream started for session %s: %s",
                    session_id,
                    frame.get("start", {}).get("mediaFormat"),
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Twilio media-stream handler crashed for %s", session_id)
    finally:
        try:
            await pump.aclose()
            await asyncio.wait_for(pump_task, timeout=10.0)
        except Exception:
            pump_task.cancel()
        try:
            await dg_connection.finish()
        except Exception:
            pass
        if LIVE_SESSIONS is not None:
            try:
                LIVE_SESSIONS.labels(provider="twilio").dec()
            except Exception:
                pass
        await _finalize_or_defer(
            session_redis=session_redis,
            session_id=session_id,
            clean_stop=clean_stop,
            generation=attempt.generation if attempt is not None else None,
            audio_seconds=pump.audio_seconds,
        )


async def _finalize_or_defer(
    *,
    session_redis: Any,
    session_id: str,
    clean_stop: bool,
    generation: Optional[int],
    audio_seconds: float,
) -> None:
    """End-of-connection policy for a live media WebSocket.

    A clean provider ``stop`` finalises immediately (dispatch batch
    analysis + clear resume state). A dirty disconnect records the
    audio position and defers finalisation behind the grace window so
    a re-attach can pick the call back up. When the resume machinery
    was unavailable, fall back to the legacy immediate dispatch.
    """
    import redis.asyncio as aioredis

    from backend.app.api.websocket import _dispatch_batch_analysis
    from backend.app.services.telephony import live_session_resume as resume

    settings = get_settings()

    if session_redis is None or generation is None:
        try:
            redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                await _dispatch_batch_analysis(redis, session_id)
            finally:
                await redis.aclose()
        except Exception:
            logger.exception("Batch analysis dispatch failed for %s", session_id)
        return

    try:
        if clean_stop:
            await _dispatch_batch_analysis(session_redis, session_id)
            await resume.clear_resume_state(session_redis, session_id)
        else:
            await resume.record_audio_position(
                session_redis, session_id, audio_seconds
            )
            resume.schedule_deferred_finalize(
                session_id=session_id,
                generation=generation,
                redis_factory=lambda: aioredis.from_url(
                    settings.REDIS_URL, decode_responses=True
                ),
                dispatch=_dispatch_batch_analysis,
            )
            logger.info(
                "Live session %s disconnected mid-call at %.1fs — finalize "
                "deferred %.0fs pending re-attach",
                session_id,
                audio_seconds,
                resume.GRACE_PERIOD_SEC,
            )
    except Exception:
        logger.exception("Batch analysis dispatch failed for %s", session_id)
    finally:
        try:
            await session_redis.aclose()
        except Exception:
            pass


def _attach_deepgram_diarization(
    dg_connection: Any,
    window: Any,
    loop: Any = None,
    call_start_offset: float = 0.0,
) -> None:
    """Wire Deepgram's live ``Transcript`` events into the per-speaker
    diarization timeline of a :class:`LiveParalinguisticWindow`.

    The SDK invokes the handler on whatever thread it chose; the window
    is **single-writer** (all mutation on the event loop), so the
    handler parses on the SDK thread and marshals the resulting turns
    onto ``loop`` via ``call_soon_threadsafe``. If the loop is already
    closed (call teardown racing a late transcript), the turns are
    dropped — diarization is best-effort.

    Silent on failures — a parsing glitch mustn't take down the audio
    ingest path.
    """
    try:
        from deepgram import LiveTranscriptionEvents  # type: ignore

        transcript_event: Any = LiveTranscriptionEvents.Transcript
    except Exception:
        # SDK not installed (tests / replay harness with a fake
        # connection) — register under the event's wire name so fakes
        # still capture the handler.
        logger.debug("deepgram LiveTranscriptionEvents not available", exc_info=True)
        transcript_event = "Results"

    from backend.app.services.paralinguistics_live import (
        diar_turns_from_deepgram_words,
    )

    def _on_transcript(self, result, **kwargs) -> None:  # noqa: ARG001
        try:
            channel = getattr(result, "channel", None)
            if channel is None and isinstance(result, dict):
                channel = (result.get("channel") or {})
            if channel is None:
                return
            alternatives = (
                getattr(channel, "alternatives", None)
                if not isinstance(channel, dict)
                else channel.get("alternatives")
            )
            if not alternatives:
                return
            alt = alternatives[0]
            words = (
                getattr(alt, "words", None)
                if not isinstance(alt, dict)
                else alt.get("words")
            )
            if not words:
                return
            # Word objects from the SDK are dataclass-ish; normalise.
            normalised: list[dict] = []
            for w in words:
                if isinstance(w, dict):
                    normalised.append(w)
                else:
                    normalised.append(
                        {
                            "speaker": getattr(w, "speaker", None),
                            "start": getattr(w, "start", None),
                            "end": getattr(w, "end", None),
                        }
                    )
            turns = diar_turns_from_deepgram_words(
                normalised, call_start_offset=call_start_offset
            )
            if not turns:
                return
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(window.update_diarization, turns)
                except RuntimeError:
                    # Loop already closed — call is tearing down; a
                    # late transcript has nowhere to go.
                    logger.debug("diarization turns dropped: loop closed")
            else:
                # No loop supplied (tests / replay harness running
                # everything on one thread) — mutate directly.
                window.update_diarization(turns)
        except Exception:
            logger.debug("deepgram diarization handler failed", exc_info=True)

    try:
        dg_connection.on(transcript_event, _on_transcript)
    except Exception:
        logger.debug(
            "could not register deepgram transcript handler", exc_info=True
        )


def _make_paralinguistic_publisher(redis_conn: Any, session_id: str):
    """Build the pump's ``publish`` callback: annotate arousal and emit
    on the session's live event channel (``live:{id}:events`` — the one
    the monitor WS and embeds actually relay). Snapshots go out as a
    ``features`` event with the data under the ``paralinguistic`` subkey
    so existing clients ignore what they don't understand.
    """

    async def _publish(features: Any) -> None:
        # Inline arousal annotation — deterministic, microsecond-cheap,
        # so live coaching can render the label on the same frame.
        try:
            from backend.app.services.paralinguistics_emotion import annotate_arousal

            annotated = annotate_arousal(features.as_dict())
        except Exception:
            annotated = features.as_dict()

        await redis_conn.publish(
            f"live:{session_id}:events",
            json.dumps({"type": "features", "paralinguistic": annotated}),
        )

    return _publish


# ── SignalWire (TwiML-compatible) ─────────────────────────────────────


@router.post("/telephony/signalwire/voice")
async def signalwire_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Inbound SignalWire webhook. SignalWire sends the same form-encoded
    payload Twilio does and signs it with HMAC-SHA1, so we reuse the
    Twilio signature helper and TwiML builder."""
    form = await request.form()
    form_dict: Dict[str, str] = {k: str(v) for k, v in form.multi_items()}

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "signalwire",
        )
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    auth_token = decrypt_token(integ.access_token) or "" if integ is not None else ""
    if not auth_token:
        auth_token = get_settings().SIGNALWIRE_TOKEN or ""

    if auth_token:
        signature = request.headers.get("X-Twilio-Signature", "")  # SignalWire reuses this header
        if not validate_twilio_signature(
            auth_token=auth_token,
            request_url=str(request.url),
            params=form_dict,
            signature_header=signature,
        ):
            raise HTTPException(status_code=403, detail="Invalid signature")

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    session = LiveSession(
        tenant_id=tenant_id,
        agent_id=tenant_id,
        source="signalwire",
        status="active",
        external_call_id=form_dict.get("CallSid") or None,
    )
    db.add(session)
    await db.flush()

    from backend.app.services.live_session_events import emit_live_session_event

    await emit_live_session_event(
        tenant_id,
        str(session.id),
        "live_session.started",
        {"source": "signalwire", "external_call_id": session.external_call_id},
    )

    base = str(request.base_url).rstrip("/").replace("http://", "wss://").replace(
        "https://", "wss://"
    )
    stream_url = f"{base}/ws/telephony/signalwire/{session.id}"
    greeting: Optional[str] = None
    if getattr(tenant, "pii_redaction_enabled", False):
        greeting = (
            "This call may be transcribed for quality assurance. "
            "Audio is not retained."
        )
    twiml = build_voice_twiml(
        session_id=str(session.id),
        stream_url=stream_url,
        greeting=greeting,
    )
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/ws/telephony/signalwire/{session_id}")
async def signalwire_media_stream(websocket: WebSocket, session_id: str):
    """SignalWire's Compatibility API streams audio in the same format
    Twilio does. Delegate to the Twilio handler."""
    await twilio_media_stream(websocket, session_id)


# ── Telnyx Call Control (inbound + live streaming only) ───────────────


_TELNYX_SESSION_KEY_PREFIX = "telephony:telnyx:call"
_TELNYX_SESSION_TTL_SECONDS = 12 * 3600


async def _telnyx_remember_session(
    call_control_id: str, session_id: uuid.UUID
) -> None:
    import redis.asyncio as aioredis

    try:
        r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            await r.set(
                f"{_TELNYX_SESSION_KEY_PREFIX}:{call_control_id}",
                str(session_id),
                ex=_TELNYX_SESSION_TTL_SECONDS,
            )
        finally:
            await r.aclose()
    except Exception:
        logger.debug("Telnyx session map write failed", exc_info=True)


async def _telnyx_lookup_session(call_control_id: str) -> Optional[uuid.UUID]:
    import redis.asyncio as aioredis

    try:
        r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            raw = await r.get(f"{_TELNYX_SESSION_KEY_PREFIX}:{call_control_id}")
        finally:
            await r.aclose()
        if not raw:
            return None
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None
    except Exception:
        logger.debug("Telnyx session map read failed", exc_info=True)
        return None


async def _telnyx_forget_session(call_control_id: str) -> None:
    import redis.asyncio as aioredis

    try:
        r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            await r.delete(f"{_TELNYX_SESSION_KEY_PREFIX}:{call_control_id}")
        finally:
            await r.aclose()
    except Exception:
        logger.debug("Telnyx session map delete failed", exc_info=True)


async def _telnyx_api_key_for_tenant(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == "telnyx")
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        return None
    return decrypt_token(integ.access_token)


async def _telnyx_public_key_for_tenant(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == "telnyx")
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        return None
    return (integ.provider_config or {}).get("public_key")


async def _telnyx_post(api_key: str, url: str, json_body: Optional[dict] = None) -> Dict:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            json=json_body or {},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Telnyx API failed: {resp.status_code} {resp.text[:300]}",
        )
    return resp.json() if resp.content else {}


@router.post("/telephony/telnyx/voice")
async def telnyx_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Telnyx Call Control webhook.

    * ``call.initiated`` — answer, start streaming audio to our WS.
    * ``call.hangup`` — dispatch batch analysis.

    Other events are acknowledged with 200 and ignored.
    """
    from backend.app.services.telephony.telnyx import (
        call_control_answer_url,
        call_control_streaming_start_url,
        streaming_start_payload,
        verify_telnyx_signature,
    )

    raw = await request.body()

    public_key = await _telnyx_public_key_for_tenant(tenant_id, db)
    if public_key:
        sig = request.headers.get("Telnyx-Signature-Ed25519", "")
        ts = request.headers.get("Telnyx-Timestamp", "")
        if not verify_telnyx_signature(
            public_key_base64=public_key,
            signature_header=sig,
            timestamp_header=ts,
            raw_body=raw,
        ):
            raise HTTPException(status_code=403, detail="Invalid Telnyx signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    data = (payload.get("data") or {}).get("payload") or payload.get("data") or {}
    event_type = (payload.get("data") or {}).get("event_type") or payload.get("event_type")
    call_control_id = data.get("call_control_id") or data.get("call_leg_id") or ""

    api_key = await _telnyx_api_key_for_tenant(tenant_id, db)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Telnyx integration not connected for this tenant",
        )

    if event_type == "call.initiated" and call_control_id:
        session = LiveSession(
            tenant_id=tenant_id,
            agent_id=tenant_id,
            source="telnyx",
            status="active",
            external_call_id=call_control_id or None,
        )
        db.add(session)
        await db.flush()

        from backend.app.services.live_session_events import (
            emit_live_session_event,
        )

        await emit_live_session_event(
            tenant_id,
            str(session.id),
            "live_session.started",
            {"source": "telnyx", "external_call_id": call_control_id},
        )

        await _telnyx_remember_session(call_control_id, session.id)

        await _telnyx_post(api_key, call_control_answer_url(call_control_id))
        http_base = str(request.base_url).rstrip("/")
        wss_base = http_base.replace("http://", "wss://").replace("https://", "wss://")
        stream_url = f"{wss_base}/ws/telephony/telnyx/{session.id}"
        await _telnyx_post(
            api_key,
            call_control_streaming_start_url(call_control_id),
            json_body=streaming_start_payload(stream_url=stream_url),
        )
        return {"status": "streaming_started", "session_id": str(session.id)}

    if event_type == "call.hangup" and call_control_id:
        session_id = await _telnyx_lookup_session(call_control_id)
        sess = None
        if session_id is not None:
            sess = await db.get(LiveSession, session_id)
        if sess is None:
            stmt = (
                select(LiveSession)
                .where(
                    LiveSession.tenant_id == tenant_id,
                    LiveSession.source == "telnyx",
                    LiveSession.ended_at.is_(None),
                )
                .order_by(LiveSession.started_at.desc())
                .limit(1)
            )
            sess = (await db.execute(stmt)).scalar_one_or_none()
        if sess is not None:
            try:
                from backend.app.api.websocket import _dispatch_batch_analysis
                import redis.asyncio as aioredis

                settings = get_settings()
                redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
                try:
                    await _dispatch_batch_analysis(redis, str(sess.id))
                    # Provider-confirmed hangup: clear any resume state
                    # so a pending grace-period finalizer stands down.
                    from backend.app.services.telephony import (
                        live_session_resume as resume,
                    )

                    await resume.clear_resume_state(redis, str(sess.id))
                finally:
                    await redis.aclose()
            except Exception:
                logger.exception("Telnyx hangup dispatch failed")

        await _telnyx_forget_session(call_control_id)
        return {"status": "hangup_handled"}

    return {"status": "ignored", "event_type": event_type}


@router.websocket("/ws/telephony/telnyx/{session_id}")
async def telnyx_media_stream(websocket: WebSocket, session_id: str):
    """Telnyx Media Streaming WebSocket. Same JSON framing as Twilio —
    delegate to the Twilio handler."""
    await twilio_media_stream(websocket, session_id)


# ── Admin: link per-tenant Twilio credentials ─────────────────────────


class TwilioCredsIn(BaseModel):
    account_sid: str = Field(..., pattern=r"^AC[a-zA-Z0-9]+$")
    auth_token: str = Field(..., min_length=8)


@router.post("/admin/integrations/twilio")
async def link_twilio_credentials(
    body: TwilioCredsIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Save the tenant's Twilio account SID + auth token.

    Used only for webhook signature verification. The auth token is
    Fernet-encrypted at rest; the SID sits in ``provider_config``.
    """
    from backend.app.services.token_crypto import encrypt_token

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == principal.tenant.id,
            Integration.provider == "twilio",
        )
        .limit(1)
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is None:
        integ = Integration(
            tenant_id=principal.tenant.id,
            user_id=principal.user_id or principal.tenant.id,
            provider="twilio",
            access_token=encrypt_token(body.auth_token),
            refresh_token=None,
            scopes=[],
            provider_config={"account_sid": body.account_sid},
        )
        db.add(integ)
        await db.flush()
    else:
        existing.access_token = encrypt_token(body.auth_token)
        cfg = dict(existing.provider_config or {})
        cfg["account_sid"] = body.account_sid
        existing.provider_config = cfg

    return {
        "provider": "twilio",
        "account_sid": body.account_sid,
        "saved": True,
    }
