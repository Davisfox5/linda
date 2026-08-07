"""Vendor meeting-bot client + orchestration (Recall.ai-compatible API).

LINDA never joins a Zoom/Meet/Teams meeting itself — it dispatches a
vendor bot that joins the meeting on our behalf and streams the
meeting's audio back to LINDA's own WebSocket ingress
(``/ws/meeting-bots/{bot_id}``, see ``backend.app.api.meeting_bots``),
which pipes it into the same Deepgram live-transcription pipeline the
telephony ingresses use (``backend.app.api.websocket`` /
``backend.app.api.audiohook``).

Vendor wire format
-------------------
We configure the bot's real-time media destination as a websocket
delivering Recall's ``audio_mixed_raw.data`` event — a JSON envelope::

    {"event": "audio_mixed_raw.data",
     "data": {"data": {"buffer": "<base64 PCM>", "timestamp": <float>},
              "bot": {"id": "<vendor bot id>"}}}

where ``buffer`` is raw 16 kHz mono S16LE (linear16) PCM, base64
encoded, sent as a WebSocket **text** frame. This is fed straight into
Deepgram live with ``encoding=linear16, sample_rate=16000`` — no
mu-law resample needed (unlike the telephony paths, which are pinned
to 8 kHz mu-law by the PSTN leg). The ingress also accepts raw
**binary** frames of the same PCM as a fallback for vendor configs
that stream binary instead of the JSON envelope; see
``backend.app.api.meeting_bots._extract_pcm``.

Bot-id chicken-and-egg
-----------------------
The real-time media destination URL must be supplied in the SAME
request that creates the bot — before the vendor has assigned it an
id. We mint our own correlation id up front (``MeetingBotJob.id``) and
use *that* as the ``{bot_id}`` path segment of the ingress URL —
exactly how ``api/telephony.py`` uses ``LiveSession.id`` in the Twilio
stream URL, minted before Twilio's side of the call exists. Once the
vendor's create-bot response carries its own id, we persist it on
``MeetingBotJob.bot_id`` and register a second Redis alias keyed by
*that* id, because the vendor's status webhooks reference the bot by
their own id, not ours.

Redis mapping
-------------
``meetingbot:{key} -> {"tenant_id", "session_id", "job_id", "token"}``
(TTL-bounded) is the ONLY way either the ingress WebSocket or the
status webhook resolves tenant context. This is deliberate, not just
a speed optimization: an inbound vendor call for either surface has no
authenticated tenant to arm Postgres RLS with, and there's no
SECURITY DEFINER resolver registered for ``meeting_bot_jobs`` keyed by
an arbitrary ``bot_id`` column (only by primary key, and only for
callers that already know the row id) — so a raw DB query filtered by
vendor bot id would return zero rows under RLS. The Redis mapping,
written by ``create_bot`` under the tenant's own already-armed
session, is what stands in for that trust boundary.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import LiveSession, MeetingBotJob, Tenant

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Meetings rarely run longer than this; a generous ceiling on how long
# the ingress/webhook correlation survives a Redis restart or an
# unusually long call.
_MEETINGBOT_REDIS_TTL_SECONDS = 6 * 3600


class MeetingBotError(Exception):
    """The vendor call failed, or returned something we can't use."""


def detect_platform(meeting_url: str) -> str:
    """Best-effort platform detection from the meeting URL host."""
    url = (meeting_url or "").lower()
    if "zoom.us" in url:
        return "zoom"
    if "meet.google.com" in url:
        return "meet"
    if "teams.microsoft.com" in url or "teams.live.com" in url:
        return "teams"
    return "unknown"


def _redis_key(key: str) -> str:
    return f"meetingbot:{key}"


async def _remember_meetingbot(
    key: str,
    *,
    tenant_id: str,
    session_id: str,
    job_id: str,
    token: str,
) -> None:
    """Write the ``meetingbot:{key}`` correlation entry.

    Called at dispatch time (before AND after the vendor call — see
    module docstring), so a write failure here means the ingress
    WebSocket / webhook can never resolve this bot. We let the
    exception propagate so ``create_bot`` can fail the dispatch loudly
    rather than silently create a bot that streams audio nobody can
    attribute to a tenant.
    """
    import redis.asyncio as aioredis

    settings = get_settings()
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await r.set(
            _redis_key(key),
            json.dumps(
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "job_id": job_id,
                    "token": token,
                }
            ),
            ex=_MEETINGBOT_REDIS_TTL_SECONDS,
        )
    finally:
        await r.aclose()


async def resolve_meetingbot(key: str) -> Optional[Dict[str, str]]:
    """Best-effort read of ``meetingbot:{key}``. ``None`` on any failure
    (Redis down, key missing, corrupt JSON) — callers treat that as
    "can't authenticate this bot"."""
    import redis.asyncio as aioredis

    settings = get_settings()
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        logger.debug("meeting-bots: redis unavailable for resolve", exc_info=True)
        return None
    try:
        raw = await r.get(_redis_key(key))
    except Exception:
        logger.debug("meeting-bots: redis get failed", exc_info=True)
        return None
    finally:
        try:
            await r.aclose()
        except Exception:
            pass
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def forget_meetingbot(key: str) -> None:
    """Best-effort delete of ``meetingbot:{key}`` on finalize."""
    import redis.asyncio as aioredis

    settings = get_settings()
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return
    try:
        await r.delete(_redis_key(key))
    except Exception:
        logger.debug("meeting-bots: redis delete failed", exc_info=True)
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


def _ws_base(settings: Any, request_base_url: Optional[str]) -> str:
    base = (settings.PUBLIC_WEBHOOK_BASE_URL or request_base_url or "").rstrip("/")
    return base.replace("http://", "ws://").replace("https://", "wss://")


def _http_base(settings: Any, request_base_url: Optional[str]) -> str:
    return (settings.PUBLIC_WEBHOOK_BASE_URL or request_base_url or "").rstrip("/")


async def _post_bot(settings: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not settings.RECALL_AI_API_KEY:
        raise MeetingBotError("RECALL_AI_API_KEY is not configured")
    base = (settings.RECALL_API_BASE or "").rstrip("/")
    url = f"{base}/api/v1/bot"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Token {settings.RECALL_AI_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise MeetingBotError(f"Recall API request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise MeetingBotError(
            f"Recall API returned {resp.status_code}: {resp.text[:300]}"
        )
    try:
        return resp.json()
    except ValueError as exc:  # pragma: no cover — defensive
        raise MeetingBotError(f"Recall API returned non-JSON body: {exc}") from exc


@dataclass
class DispatchResult:
    job: MeetingBotJob
    session: LiveSession
    ingress_token: str


async def create_bot(
    db: AsyncSession,
    *,
    tenant: Tenant,
    meeting_url: str,
    requested_by_user_id: Optional[uuid.UUID],
    request_base_url: str,
) -> DispatchResult:
    """Dispatch a vendor bot to ``meeting_url`` and persist the job/session.

    Callers must already be running inside the tenant's armed RLS
    context (true for every authenticated router dependency chain in
    this codebase — see ``auth.get_current_principal``).

    On vendor failure, the ``MeetingBotJob``/``LiveSession`` rows are
    still committed (marked ``failed``) before :class:`MeetingBotError`
    propagates, so a failed dispatch attempt still shows up in the
    tenant's job list instead of vanishing.
    """
    settings = get_settings()
    platform = detect_platform(meeting_url)

    session = LiveSession(
        tenant_id=tenant.id,
        agent_id=requested_by_user_id or tenant.id,
        source="meeting_bot",
        status="active",
    )
    db.add(session)
    await db.flush()

    job = MeetingBotJob(
        tenant_id=tenant.id,
        live_session_id=session.id,
        requested_by_user_id=requested_by_user_id,
        provider="recall",
        meeting_url=meeting_url,
        platform=platform,
        status="requested",
    )
    db.add(job)
    await db.flush()

    ingress_token = secrets.token_urlsafe(32)
    ws_url = (
        f"{_ws_base(settings, request_base_url)}/api/v1/ws/meeting-bots/{job.id}"
        f"?token={ingress_token}"
    )
    webhook_url = f"{_http_base(settings, request_base_url)}/api/v1/meeting-bots/webhook"

    # Registered BEFORE the vendor call: the ingress WS is our own id, so
    # it must resolve the instant the vendor connects (which can race the
    # HTTP response coming back to us).
    await _remember_meetingbot(
        str(job.id),
        tenant_id=str(tenant.id),
        session_id=str(session.id),
        job_id=str(job.id),
        token=ingress_token,
    )

    payload = {
        "meeting_url": meeting_url,
        "bot_name": "LINDA Notetaker",
        "recording_config": {
            "realtime_endpoints": [
                {
                    "type": "websocket",
                    "url": ws_url,
                    "events": ["audio_mixed_raw.data"],
                }
            ],
            "audio_mixed_raw": {},
        },
        "metadata": {
            "tenant_id": str(tenant.id),
            "job_id": str(job.id),
            "session_id": str(session.id),
        },
        "webhook_url": webhook_url,
    }

    try:
        vendor_bot = await _post_bot(settings, payload)
    except MeetingBotError as exc:
        job.status = "failed"
        job.last_error = str(exc)[:500]
        session.status = "completed"
        await db.commit()
        raise

    vendor_bot_id = str(vendor_bot.get("id") or "") or None
    job.status = "joining"
    job.bot_id = vendor_bot_id
    job.payload = {"vendor_bot": vendor_bot, "ingress_id": str(job.id)}
    session.external_call_id = vendor_bot_id
    await db.commit()
    await db.refresh(job)

    if vendor_bot_id:
        # Alias so the vendor's status webhooks (which reference the
        # meeting by THEIR id, not ours) resolve to the same context.
        await _remember_meetingbot(
            vendor_bot_id,
            tenant_id=str(tenant.id),
            session_id=str(session.id),
            job_id=str(job.id),
            token=ingress_token,
        )

    return DispatchResult(job=job, session=session, ingress_token=ingress_token)


async def stop_bot(job: MeetingBotJob) -> None:
    """Ask the vendor to leave the call. Best-effort from the caller's
    point of view — the router still finalizes the job/session locally
    even when this raises."""
    if not job.bot_id:
        return
    settings = get_settings()
    if not settings.RECALL_AI_API_KEY:
        raise MeetingBotError("RECALL_AI_API_KEY is not configured")
    base = (settings.RECALL_API_BASE or "").rstrip("/")
    url = f"{base}/api/v1/bot/{job.bot_id}/leave_call"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Token {settings.RECALL_AI_API_KEY}"}
            )
    except httpx.HTTPError as exc:
        raise MeetingBotError(f"Recall API leave_call failed: {exc}") from exc
    if resp.status_code >= 400 and resp.status_code != 404:
        raise MeetingBotError(
            f"Recall API leave_call returned {resp.status_code}: {resp.text[:300]}"
        )


__all__ = [
    "MeetingBotError",
    "DispatchResult",
    "detect_platform",
    "create_bot",
    "stop_bot",
    "resolve_meetingbot",
    "forget_meetingbot",
]
