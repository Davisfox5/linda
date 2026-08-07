"""SIPREC bridge — turn SRS sidecar frames into transcription dispatches.

The Session Recording Server (FreeSWITCH userspace, see
``services/telephony/siprec_srs``) terminates SIP and SRTP. It then
forwards two kinds of events to this Python process over a local
control channel:

* **Lifecycle events** — ``recording.started`` / ``recording.stopped``
  / ``participant.joined``. These come in as JSON objects on the
  ``POST /siprec/events`` endpoint (see ``api/siprec.py``) and are
  applied to the ``SiprecSession`` row by ``SiprecBridge.handle_event``.
* **Audio frames** — μ-law 8 kHz or PCM16 8 kHz payloads keyed by the
  recording session id and SDP ``a=label`` (which resolves to a
  participant via the rs-metadata). Frames go through
  ``SiprecBridge.handle_audio`` and out to the transcription pipeline.

Why an injectable dispatcher? Live transcription in this codebase
talks to Deepgram via a long-lived WS connection per call (see the
Twilio path in ``api/telephony.py``). Production wiring spins one of
those up per ``SiprecSession``. Tests inject a recording mock so we
can assert "frames N through M reached the pipeline in order" without
spinning up Deepgram. The ``TranscriptionDispatch`` Protocol is the
contract.

Multi-worker durability (challenge #2c/C4)
==========================================

The API runs more than one uvicorn worker, and the SRS delivers every
event as an independent HTTP POST — ``recording.started`` can land on
worker A and the audio frames on worker B. Session state therefore
lives in **Redis** when a ``redis_factory`` is injected:

* ``siprec:sess:{rec_id}`` — JSON {tenant_id, live_session_id,
  provider}, TTL = idle timeout, refreshed on every frame. TTL expiry
  IS the idle-timeout enforcement; the reaper finalises the DB rows.
* ``siprec:seq:{rec_id}`` — hash of label → last sequence seen, for
  cross-worker duplicate rejection. The check-then-set is not atomic
  across workers; the worst case of that race is a duplicate frame
  reaching the transcriber, which is tolerable.
* ``siprec:claim:{rec_id}`` — short-lived SET NX claim so exactly one
  worker persists the DB rows for a ``recording.started`` retry burst;
  losers poll for the session key instead of double-inserting.

Workers that see frames for a session they didn't open lazily open
their own local transcription dispatch (per-process Deepgram WS).
Without a ``redis_factory`` the bridge degrades to the original
in-process behaviour (used by unit tests and single-worker dev).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.audio import AudioFormat, to_mulaw_8k

logger = logging.getLogger(__name__)


# ── Frame model ─────────────────────────────────────────────────────────


@dataclass
class SiprecAudioFrame:
    """One audio frame forwarded by the SRS sidecar.

    ``recording_session_id`` is the id from the rs-metadata
    ``<recording session_id="...">`` attribute (matches the
    ``SiprecSession.src_session_id`` column). ``label`` is the SDP
    ``a=label`` value of the media stream this frame belongs to —
    typically ``"1"`` for the caller's audio and ``"2"`` for the
    callee's, but vendors are free to pick any stable identifier.

    Audio is **already plaintext** — the SRS terminated SRTP. The
    bridge only handles format normalization, not decryption.
    """

    recording_session_id: str
    label: str  # SDP a=label
    sequence: int  # monotonically increasing per (session, label)
    audio_format: AudioFormat
    payload: bytes
    received_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── Transcription dispatch contract ─────────────────────────────────────


class TranscriptionDispatch(Protocol):
    """Per-session transcription sink the bridge writes audio into.

    Production implementation opens a Deepgram live WS in
    ``open_session`` and closes it in ``close_session``. Tests use a
    record-everything mock.

    ``send_audio`` is awaited on every frame and must not block
    indefinitely — frames are dispatched inline from the webhook
    handler, so backpressure here translates to slow SRS responses
    and, past the SRS's own buffer, dropped frames at the edge. The
    production Deepgram client is non-blocking; if a custom dispatch
    needs to do I/O, queue it internally.
    """

    async def open_session(
        self,
        recording_session_id: str,
        live_session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider: str,
    ) -> None: ...

    async def send_audio(
        self,
        recording_session_id: str,
        label: str,
        audio_mulaw_8k: bytes,
    ) -> None: ...

    async def close_session(
        self,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None: ...


class _NullDispatch:
    """Drop-everything dispatch — the default when nothing is wired up.

    Used by smoke tests and in environments where the SRS is
    deployed but transcription wiring isn't ready yet. Logs at INFO
    once per recording session so operators notice the no-op.
    """

    def __init__(self) -> None:
        self._opened: set[str] = set()

    async def open_session(
        self,
        recording_session_id: str,
        live_session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider: str,
    ) -> None:
        if recording_session_id not in self._opened:
            logger.info(
                "SIPREC bridge: NullDispatch.open_session "
                "rec=%s tenant=%s provider=%s — frames will be discarded",
                recording_session_id,
                tenant_id,
                provider,
            )
            self._opened.add(recording_session_id)

    async def send_audio(
        self,
        recording_session_id: str,
        label: str,
        audio_mulaw_8k: bytes,
    ) -> None:
        return None

    async def close_session(
        self,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None:
        self._opened.discard(recording_session_id)


# ── Persistence callbacks (injected so the bridge can be tested without DB)


SessionFactory = Callable[[], "AsyncSession"]
RedisFactory = Callable[[], Any]


def _sess_key(recording_session_id: str) -> str:
    return f"siprec:sess:{recording_session_id}"


def _seq_key(recording_session_id: str) -> str:
    return f"siprec:seq:{recording_session_id}"


def _claim_key(recording_session_id: str) -> str:
    return f"siprec:claim:{recording_session_id}"


class SiprecBridge:
    """Orchestrates lifecycle events + audio frames for SIPREC sessions.

    One instance per worker process. Shared session state lives in
    Redis (see module docstring); ``self._sessions`` is only a local
    read cache plus the fallback store when no ``redis_factory`` is
    injected.
    """

    # How long a recording.started claim may be held before another
    # worker may retry the insert (covers a worker dying mid-insert).
    _CLAIM_TTL_SECONDS = 30
    # How long losers of the claim race poll for the winner's state.
    _CLAIM_WAIT_SECONDS = 5.0
    # How stale a local cache entry may get before it is revalidated
    # against Redis (catches sessions stopped/expired on other workers).
    _CACHE_REVALIDATE_SECONDS = 30.0

    def __init__(
        self,
        dispatch: Optional[TranscriptionDispatch] = None,
        session_factory: Optional[SessionFactory] = None,
        idle_timeout_seconds: float = 600.0,
        redis_factory: Optional[RedisFactory] = None,
    ) -> None:
        self._dispatch: TranscriptionDispatch = dispatch or _NullDispatch()
        # ``session_factory`` is the project's async-session maker
        # (``backend.app.db.async_session``). Tests pass a custom
        # factory bound to the in-memory SQLite DB.
        self._session_factory = session_factory
        self._sessions: Dict[str, _SessionState] = {}
        self._lock = asyncio.Lock()
        self._idle_timeout = idle_timeout_seconds
        self._redis_factory = redis_factory
        self._redis: Optional[Any] = None
        # recording_session_ids whose dispatch THIS worker has opened
        # (the opener via handle_started, others lazily on first frame).
        self._dispatch_opened: Set[str] = set()
        # In-flight handle_started claims for the in-memory path, so
        # two concurrent started events in one process can't both
        # insert (the old check-then-act race).
        self._creating: Dict[str, "asyncio.Future"] = {}

    async def _get_redis(self) -> Optional[Any]:
        if self._redis_factory is None:
            return None
        if self._redis is None:
            self._redis = self._redis_factory()
        return self._redis

    # ── Lifecycle ───────────────────────────────────────────────────

    async def handle_started(
        self,
        *,
        recording_session_id: str,
        tenant_id: uuid.UUID,
        provider: str,
        agent_user_id: Optional[uuid.UUID],
        src_call_id: Optional[str],
        src_metadata: Dict[str, Any],
        is_consent_attested: bool,
        sdp_crypto_suite: Optional[str] = None,
    ) -> "_SessionState":
        """Apply a ``recording.started`` event from the SRS.

        Creates a ``LiveSession`` row (sibling to the existing
        Twilio/SignalWire/Telnyx live sessions, so the rest of the
        coaching pipeline treats SIPREC like any other live audio
        source) and a ``SiprecSession`` row that tracks
        SIPREC-specific metadata. Idempotent on
        ``recording_session_id`` — re-firing the same event (SRS
        retries, or the retry landing on a different worker) returns
        the existing state without inserting twice.
        """

        redis = await self._get_redis()
        if redis is not None:
            return await self._handle_started_redis(
                redis,
                recording_session_id=recording_session_id,
                tenant_id=tenant_id,
                provider=provider,
                agent_user_id=agent_user_id,
                src_call_id=src_call_id,
                src_metadata=src_metadata,
                is_consent_attested=is_consent_attested,
                sdp_crypto_suite=sdp_crypto_suite,
            )

        # ── In-memory path (tests / single worker) ──────────────────
        # Hold the claim under ONE lock acquisition: the first caller
        # installs a future, concurrent callers await it. This closes
        # the old check-then-act window where two started retries both
        # passed the existence check and both inserted DB rows.
        async with self._lock:
            existing = self._sessions.get(recording_session_id)
            if existing is not None:
                return existing
            pending = self._creating.get(recording_session_id)
            if pending is None:
                owner = True
                pending = asyncio.get_event_loop().create_future()
                self._creating[recording_session_id] = pending
            else:
                owner = False
        if not owner:
            return await asyncio.shield(pending)

        try:
            state = await self._create_session(
                recording_session_id=recording_session_id,
                tenant_id=tenant_id,
                provider=provider,
                agent_user_id=agent_user_id,
                src_call_id=src_call_id,
                src_metadata=src_metadata,
                is_consent_attested=is_consent_attested,
                sdp_crypto_suite=sdp_crypto_suite,
            )
        except BaseException as exc:
            async with self._lock:
                self._creating.pop(recording_session_id, None)
            if not pending.done():
                pending.set_exception(exc)
                # The exception is re-raised to our own caller below;
                # if nobody else awaited the future, don't warn about
                # an unretrieved exception.
                pending.exception()
            raise
        async with self._lock:
            self._sessions[recording_session_id] = state
            self._creating.pop(recording_session_id, None)
        if not pending.done():
            pending.set_result(state)
        return state

    async def _handle_started_redis(
        self,
        redis: Any,
        *,
        recording_session_id: str,
        tenant_id: uuid.UUID,
        provider: str,
        agent_user_id: Optional[uuid.UUID],
        src_call_id: Optional[str],
        src_metadata: Dict[str, Any],
        is_consent_attested: bool,
        sdp_crypto_suite: Optional[str],
    ) -> "_SessionState":
        cached = await self._load_state(redis, recording_session_id)
        if cached is not None:
            return cached

        claimed = await redis.set(
            _claim_key(recording_session_id),
            "1",
            nx=True,
            ex=self._CLAIM_TTL_SECONDS,
        )
        if not claimed:
            # Another worker is inserting right now — wait for its
            # state to appear rather than double-inserting.
            deadline = asyncio.get_event_loop().time() + self._CLAIM_WAIT_SECONDS
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.1)
                state = await self._load_state(redis, recording_session_id)
                if state is not None:
                    return state
            raise RuntimeError(
                f"SIPREC handle_started: claim for {recording_session_id} "
                "not resolved within wait window"
            )

        try:
            state = await self._create_session(
                recording_session_id=recording_session_id,
                tenant_id=tenant_id,
                provider=provider,
                agent_user_id=agent_user_id,
                src_call_id=src_call_id,
                src_metadata=src_metadata,
                is_consent_attested=is_consent_attested,
                sdp_crypto_suite=sdp_crypto_suite,
            )
            await redis.set(
                _sess_key(recording_session_id),
                json.dumps(
                    {
                        "tenant_id": str(tenant_id),
                        "live_session_id": str(state.live_session_id),
                        "provider": provider,
                    }
                ),
                ex=int(self._idle_timeout),
            )
        finally:
            try:
                await redis.delete(_claim_key(recording_session_id))
            except Exception:
                pass
        state.cached_at = asyncio.get_event_loop().time()
        async with self._lock:
            self._sessions[recording_session_id] = state
        return state

    async def _create_session(
        self,
        *,
        recording_session_id: str,
        tenant_id: uuid.UUID,
        provider: str,
        agent_user_id: Optional[uuid.UUID],
        src_call_id: Optional[str],
        src_metadata: Dict[str, Any],
        is_consent_attested: bool,
        sdp_crypto_suite: Optional[str],
    ) -> "_SessionState":
        live_session_id = await self._persist_started(
            tenant_id=tenant_id,
            provider=provider,
            agent_user_id=agent_user_id,
            recording_session_id=recording_session_id,
            src_call_id=src_call_id,
            src_metadata=src_metadata,
            is_consent_attested=is_consent_attested,
            sdp_crypto_suite=sdp_crypto_suite,
        )

        await self._dispatch.open_session(
            recording_session_id=recording_session_id,
            live_session_id=live_session_id,
            tenant_id=tenant_id,
            provider=provider,
        )
        self._dispatch_opened.add(recording_session_id)

        return _SessionState(
            recording_session_id=recording_session_id,
            tenant_id=tenant_id,
            provider=provider,
            live_session_id=live_session_id,
            sequence_seen={},
        )

    async def _load_state(
        self, redis: Any, recording_session_id: str
    ) -> Optional["_SessionState"]:
        """Local cache first (revalidated periodically), then Redis
        (frames landing on a worker that didn't see
        ``recording.started``)."""
        now = asyncio.get_event_loop().time()
        async with self._lock:
            state = self._sessions.get(recording_session_id)
        if state is not None:
            if now - state.cached_at < self._CACHE_REVALIDATE_SECONDS:
                return state
            # Stale — confirm the session still exists in Redis (it may
            # have been stopped or idle-expired via another worker).
            alive = await redis.get(_sess_key(recording_session_id))
            if alive:
                state.cached_at = now
                return state
            async with self._lock:
                self._sessions.pop(recording_session_id, None)
            self._dispatch_opened.discard(recording_session_id)
            return None
        raw = await redis.get(_sess_key(recording_session_id))
        if not raw:
            return None
        try:
            doc = json.loads(raw)
            state = _SessionState(
                recording_session_id=recording_session_id,
                tenant_id=uuid.UUID(doc["tenant_id"]),
                provider=str(doc.get("provider") or "siprec"),
                live_session_id=uuid.UUID(doc["live_session_id"]),
                sequence_seen={},
                cached_at=now,
            )
        except Exception:
            logger.exception(
                "SIPREC session state in Redis is malformed for %s",
                recording_session_id,
            )
            return None
        async with self._lock:
            self._sessions[recording_session_id] = state
        return state

    async def handle_stopped(
        self,
        *,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None:
        """Apply a ``recording.stopped`` event from the SRS."""

        async with self._lock:
            state = self._sessions.pop(recording_session_id, None)

        live_session_id = state.live_session_id if state else None
        redis = await self._get_redis()
        if redis is not None:
            try:
                if live_session_id is None:
                    raw = await redis.get(_sess_key(recording_session_id))
                    if raw:
                        try:
                            live_session_id = uuid.UUID(
                                json.loads(raw)["live_session_id"]
                            )
                        except Exception:
                            pass
                await redis.delete(_sess_key(recording_session_id))
                await redis.delete(_seq_key(recording_session_id))
            except Exception:
                logger.exception(
                    "SIPREC redis cleanup failed for %s", recording_session_id
                )

        self._dispatch_opened.discard(recording_session_id)
        await self._dispatch.close_session(
            recording_session_id=recording_session_id, reason=reason
        )
        await self._persist_stopped(
            recording_session_id=recording_session_id,
            live_session_id=live_session_id,
            reason=reason,
        )

    # ── Audio ───────────────────────────────────────────────────────

    async def handle_audio(self, frame: SiprecAudioFrame) -> bool:
        """Normalize one audio frame and dispatch it.

        Returns ``True`` when the frame was forwarded to the
        transcription dispatch, ``False`` when it was dropped (no
        active session, duplicate sequence, or malformed payload).
        Drops are logged at DEBUG so a noisy SBC doesn't spam the
        logs.
        """

        if not frame.payload:
            return False

        redis = await self._get_redis()
        if redis is not None:
            state = await self._load_state(redis, frame.recording_session_id)
        else:
            async with self._lock:
                state = self._sessions.get(frame.recording_session_id)
        if state is None:
            logger.debug(
                "SIPREC audio frame for unknown session %s (label=%s seq=%s) — dropping",
                frame.recording_session_id,
                frame.label,
                frame.sequence,
            )
            return False

        # Per-stream monotonic sequence guard. SRS deliveries can
        # duplicate on retry; rejecting <= last_seen prevents
        # double-feeding the transcriber. With Redis the guard is
        # shared across workers (check-then-set, not atomic — the
        # worst case of the race is one duplicate frame, tolerable).
        if redis is not None:
            try:
                last_raw = await redis.hget(_seq_key(frame.recording_session_id), frame.label)
                if last_raw is not None and frame.sequence <= int(last_raw):
                    return False
                await redis.hset(
                    _seq_key(frame.recording_session_id),
                    frame.label,
                    frame.sequence,
                )
                # Activity refreshes the idle timeout — TTL expiry is
                # how an SRS that died without recording.stopped gets
                # cleaned up (see reap_stale_sessions).
                await redis.expire(
                    _sess_key(frame.recording_session_id), int(self._idle_timeout)
                )
                await redis.expire(
                    _seq_key(frame.recording_session_id), int(self._idle_timeout)
                )
            except Exception:
                logger.exception("SIPREC sequence guard failed — dropping frame")
                return False
        else:
            last = state.sequence_seen.get(frame.label, -1)
            if frame.sequence <= last:
                return False
            state.sequence_seen[frame.label] = frame.sequence

        try:
            audio = to_mulaw_8k(frame.payload, frame.audio_format)
        except Exception:
            logger.exception(
                "SIPREC audio normalization failed for rec=%s label=%s",
                frame.recording_session_id,
                frame.label,
            )
            return False

        # A worker that never saw recording.started still needs a
        # local transcription session (per-process Deepgram WS).
        if frame.recording_session_id not in self._dispatch_opened:
            try:
                await self._dispatch.open_session(
                    recording_session_id=frame.recording_session_id,
                    live_session_id=state.live_session_id,
                    tenant_id=state.tenant_id,
                    provider=state.provider,
                )
            except Exception:
                logger.exception(
                    "SIPREC lazy dispatch open failed for %s",
                    frame.recording_session_id,
                )
                return False
            self._dispatch_opened.add(frame.recording_session_id)

        await self._dispatch.send_audio(
            recording_session_id=frame.recording_session_id,
            label=frame.label,
            audio_mulaw_8k=audio,
        )
        return True

    # ── Idle reaping ────────────────────────────────────────────────

    async def reap_stale_sessions(self) -> int:
        """Finalise DB rows for sessions whose Redis state expired.

        The session key's TTL is the idle timeout (refreshed on every
        frame). If the SRS dies without sending ``recording.stopped``,
        the key expires and the ``SiprecSession``/``LiveSession`` rows
        are left open — this reaper closes them. Only rows older than
        the idle timeout are considered, so a session that was just
        claimed but hasn't written Redis yet can't be reaped.

        Returns the number of sessions reaped. Safe to run on every
        worker concurrently — finalisation is idempotent.
        """
        redis = await self._get_redis()
        if redis is None:
            return 0

        # Local sweep first: drop cache entries (and close local
        # dispatch sessions) for sessions that vanished from Redis —
        # stopped or idle-expired via another worker.
        async with self._lock:
            cached_ids = list(self._sessions.keys())
        for rec_id in cached_ids:
            try:
                alive = await redis.get(_sess_key(rec_id))
            except Exception:
                logger.exception("SIPREC reaper redis check failed")
                continue
            if alive:
                continue
            async with self._lock:
                self._sessions.pop(rec_id, None)
            if rec_id in self._dispatch_opened:
                self._dispatch_opened.discard(rec_id)
                try:
                    await self._dispatch.close_session(
                        recording_session_id=rec_id, reason="idle_timeout"
                    )
                except Exception:
                    logger.exception("SIPREC reaper dispatch close failed")

        if self._session_factory is None:
            return 0

        from backend.app.models import SiprecSession

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._idle_timeout)
        reaped = 0
        async with self._session_factory() as db:
            stmt = select(SiprecSession).where(
                SiprecSession.ended_at.is_(None),
                SiprecSession.started_at < cutoff,
            )
            rows = (await db.execute(stmt)).scalars().all()

        for row in rows:
            try:
                alive = await redis.get(_sess_key(row.src_session_id))
            except Exception:
                logger.exception("SIPREC reaper redis check failed")
                continue
            if alive:
                continue  # still active — frames are refreshing the TTL
            logger.warning(
                "SIPREC session %s idle-expired without recording.stopped — reaping",
                row.src_session_id,
            )
            await self.handle_stopped(
                recording_session_id=row.src_session_id,
                reason="idle_timeout",
            )
            reaped += 1
        return reaped

    # ── Introspection (for /admin and tests) ────────────────────────

    def active_sessions(self) -> List[str]:
        return list(self._sessions.keys())

    def get_state(self, recording_session_id: str) -> Optional["_SessionState"]:
        return self._sessions.get(recording_session_id)

    # ── Persistence (overridable hooks) ─────────────────────────────

    async def _persist_started(
        self,
        *,
        tenant_id: uuid.UUID,
        provider: str,
        agent_user_id: Optional[uuid.UUID],
        recording_session_id: str,
        src_call_id: Optional[str],
        src_metadata: Dict[str, Any],
        is_consent_attested: bool,
        sdp_crypto_suite: Optional[str],
    ) -> uuid.UUID:
        """Insert ``LiveSession`` + ``SiprecSession`` rows.

        Returns the ``LiveSession.id`` so the dispatcher can correlate
        downstream artefacts (transcripts, scorecards) back to the
        same session row the existing live-coaching UI reads from.
        """

        if self._session_factory is None:
            # Tests that don't need DB persistence pass
            # ``session_factory=None`` and rely on the dispatch mock
            # alone. We still mint a live-session uuid so the rest
            # of the bridge's interface is consistent.
            return uuid.uuid4()

        # Late import — keeps the test path that injects a None
        # session_factory free of SQLAlchemy mapping side-effects.
        from backend.app.models import LiveSession, SiprecSession

        async with self._session_factory() as db:
            live = LiveSession(
                tenant_id=tenant_id,
                # ``LiveSession.agent_id`` is non-nullable; SIPREC
                # sometimes can't resolve a LINDA user from the SBC's
                # AOR (e.g. shared-line scenarios). Callers MUST
                # resolve a default "siprec service" user up the
                # stack — we surface a clear error here rather than
                # writing a fake uuid.
                agent_id=agent_user_id or _require_agent_user_id(),
                source=provider,
                status="live",
                external_call_id=src_call_id or recording_session_id,
                started_at=datetime.now(timezone.utc),
            )
            db.add(live)
            await db.flush()
            siprec = SiprecSession(
                tenant_id=tenant_id,
                live_session_id=live.id,
                provider=provider,
                src_session_id=recording_session_id,
                src_call_id=src_call_id,
                src_metadata=src_metadata,
                is_consent_attested=is_consent_attested,
                sdp_crypto_suite=sdp_crypto_suite,
                started_at=datetime.now(timezone.utc),
            )
            db.add(siprec)
            await db.commit()

            from backend.app.services.live_session_events import (
                emit_live_session_event,
            )

            await emit_live_session_event(
                tenant_id,
                str(live.id),
                "live_session.started",
                {
                    "source": provider,
                    "external_call_id": src_call_id or recording_session_id,
                },
            )
            return live.id

    async def _persist_stopped(
        self,
        *,
        recording_session_id: str,
        live_session_id: Optional[uuid.UUID],
        reason: Optional[str],
    ) -> None:
        if self._session_factory is None:
            return
        from backend.app.models import LiveSession, SiprecSession

        async with self._session_factory() as db:
            stmt = select(SiprecSession).where(
                SiprecSession.src_session_id == recording_session_id
            )
            siprec = (await db.execute(stmt)).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if siprec is not None:
                siprec.ended_at = now
                if reason:
                    siprec.end_reason = reason
            if live_session_id is not None:
                live = await db.get(LiveSession, live_session_id)
                if live is not None:
                    live.status = "ended"
                    live.ended_at = now
            await db.commit()


def _require_agent_user_id() -> uuid.UUID:
    """Helper that fails loudly when an SRS event has no resolved user.

    Centralised here so the error message is consistent — failure to
    resolve a user is the single most common deployment misconfig
    on this path (the SBC's From-AOR doesn't match a User.email in
    LINDA), and a clear message saves an hour of triage.
    """

    raise ValueError(
        "SIPREC handle_started: agent_user_id is None. The SRS event "
        "did not resolve to a LINDA user; configure a default "
        "service-account user via SIPREC_SERVICE_USER_ID or pre-create "
        "the user with the SBC's From-AOR before enabling recording."
    )


# ── Internal session state ──────────────────────────────────────────────


@dataclass
class _SessionState:
    """In-memory state for one active SIPREC recording session."""

    recording_session_id: str
    tenant_id: uuid.UUID
    provider: str
    live_session_id: uuid.UUID
    sequence_seen: Dict[str, int]
    # Monotonic timestamp of the last Redis validation of this cache
    # entry (0.0 = never). Lets other workers notice a session that
    # was stopped or idle-expired elsewhere.
    cached_at: float = 0.0


# ── Module singleton ────────────────────────────────────────────────────


_singleton: Optional[SiprecBridge] = None


def get_bridge() -> SiprecBridge:
    """Return the process-wide bridge (lazy init).

    Wired up at first use rather than at import time so test code
    can replace it with a stub via ``set_bridge`` before any HTTP
    handler resolves it.
    """

    global _singleton
    if _singleton is None:
        from backend.app.config import get_settings
        from backend.app.db import async_session

        def _redis_factory() -> Any:
            import redis.asyncio as aioredis

            return aioredis.from_url(
                get_settings().REDIS_URL, decode_responses=True
            )

        # Real transcription sink: Deepgram live → Redis buffer → the
        # shared voice pipeline. Without this the bridge falls back to
        # _NullDispatch and silently discards every SIPREC audio frame.
        from backend.app.services.telephony.siprec.dispatch import (
            DeepgramSiprecDispatch,
        )

        _singleton = SiprecBridge(
            dispatch=DeepgramSiprecDispatch(),
            session_factory=async_session,
            redis_factory=_redis_factory,
        )
    return _singleton


def set_bridge(bridge: Optional[SiprecBridge]) -> None:
    """Override (or reset) the process-wide bridge — tests only."""

    global _singleton
    _singleton = bridge
