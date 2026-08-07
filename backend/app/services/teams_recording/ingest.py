"""Persist Teams compliance-recording events into LINDA's existing
recording pipeline.

Two producers feed this module:

* ``api/teams_recording.py``'s ``POST /teams/notification`` handler —
  one call to :func:`ingest_notification` per parsed
  :class:`~backend.app.services.teams_recording.subscriptions.ChangeNotification`.
* The same file's ``POST /teams/bot/callback`` handler — one call to
  :func:`ingest_bot_event` per parsed
  :class:`~backend.app.services.teams_recording.bot_callback.BotCallbackEvent`.

Both producers resolve a customer's Azure AD tenant id to a LINDA
``Integration`` row (``provider="teams_compliance"``,
``provider_config["aad_tenant_id"]``) — that row is what
``services/teams_recording/teams_graph.bootstrap_teams_integration``
creates during subscription bootstrap. Once resolved we bind the
tenant (:func:`backend.app.tenant_ctx.bind_tenant_async`) before
touching any tenant-scoped table, mirroring
``api/uc_telephony.py``'s ``_resolve_integration`` pattern — reading
``integrations`` itself is allowed before the tenant is bound
(``AUTH_BOOTSTRAP_TABLES`` in ``rls.py``; provider callbacks are the
documented reason that table is on the bootstrap list) but nothing
downstream is.

Two distinct outcomes, matching the two Graph resources we subscribe
to (see ``subscriptions.SUPPORTED_RESOURCES``) and the two bot-callback
event families:

* Metadata-only observations (``communications/callRecords`` change
  notifications, and bot ``session.started`` / ``session.stopped``
  events) — upsert a :class:`~backend.app.models.TeamsCallRecord` row.
  There's no recording to fetch yet, so nothing is dispatched to
  Celery. This is the "persist and mark for batch" half of the
  contract.
* Recording-bearing events (``communications/onlineMeetings/getAllRecordings``
  change notifications, and bot ``audio.available`` events) — these
  DO have (or point at) real audio, so they ride the existing recording
  pipeline:
    - Graph notifications upsert a :class:`~backend.app.models.UcRecordingJob`
      (``provider="teams_compliance"``) and enqueue the existing
      ``fetch_uc_recording`` Celery task — the same idempotent
      upsert-then-dispatch shape as ``api/uc_telephony.py``. The actual
      audio bytes are fetched later, inside that task, using an
      app-only Graph bearer (see
      ``services/telephony/uc/teams_compliance.py``).
    - Bot ``audio.available`` events already carry a concrete
      ``audio_url`` (wherever the .NET bot staged the audio, e.g. Azure
      Blob). There's nothing to fetch — we bridge straight into the
      live pipeline the same way ``POST /interactions/ingest-recording``'s
      ``audio_url`` mode does: point an ``Interaction.audio_url`` at it
      and dispatch ``process_voice_interaction`` directly. No
      ``UcRecordingJob`` involved for this path.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Integration, Interaction, TeamsCallRecord, UcRecordingJob
from backend.app.services.teams_recording.bot_callback import (
    AUDIO_AVAILABLE,
    SESSION_STARTED,
    SESSION_STOPPED,
    BotCallbackEvent,
)
from backend.app.services.teams_recording.subscriptions import ChangeNotification
from backend.app.services.telephony.uc.base import UCWebhookEvent
from backend.app.tenant_ctx import bind_tenant_async

logger = logging.getLogger(__name__)

# Matches the reserved TelephonyProvider literal in
# services/telephony/__init__.py — keep in sync with that namespace,
# not with any ad-hoc "teams" string.
TEAMS_PROVIDER = "teams_compliance"

_CALL_RECORD_RESOURCE_RE = re.compile(r"^communications/callRecords/(?P<call_id>.+)$")
RECORDING_RESOURCE = "communications/onlineMeetings/getAllRecordings"
_ONLINE_MEETING_RECORDING_RE = re.compile(
    r"^communications/onlineMeetings/(?P<meeting_id>[^/]+)/recordings/(?P<recording_id>[^/]+)$"
)


# ── Tenant resolution ─────────────────────────────────────────────────


async def resolve_teams_integration(
    db: AsyncSession, *, aad_tenant_id: Optional[str]
) -> Optional[Integration]:
    """Map a customer's Azure AD tenant id to their ``teams_compliance``
    ``Integration`` row.

    Scans by provider then filters in Python rather than a JSONB
    containment query — the number of Teams-compliance customers is
    small (one row per onboarded tenant), and a plain Python filter
    works identically against the Postgres and SQLite (unit test)
    backends, unlike a Postgres-only JSONB operator.

    Returns ``None`` when unresolved (unknown/not-yet-bootstrapped
    tenant) rather than raising — a single Graph batch or bot session
    can't fail wholesale just because one entry belongs to a tenant we
    haven't finished onboarding.
    """
    if not aad_tenant_id:
        return None
    rows = (
        await db.execute(select(Integration).where(Integration.provider == TEAMS_PROVIDER))
    ).scalars().all()
    for integ in rows:
        if (integ.provider_config or {}).get("aad_tenant_id") == aad_tenant_id:
            return integ
    return None


# ── Graph change-notification ingestion ────────────────────────────────


def notification_to_uc_event(notification: ChangeNotification) -> UCWebhookEvent:
    """Project a recording-resource ``ChangeNotification`` into the
    shared ``UCWebhookEvent`` shape the UC fetch pipeline expects.

    Graph's notification envelope doesn't carry a directly-fetchable
    content URL — only the resource's ``@odata.id`` (e.g.
    ``communications/onlineMeetings/{meetingId}/recordings/{recordingId}``).
    ``recording_url`` is left ``None`` here; the ``teams_compliance``
    UC provider builds the actual Graph content URL from ``raw['odata_id']``
    at fetch time (see ``services/telephony/uc/teams_compliance.py``).
    """
    resource_data = notification.raw.get("resourceData") or {}
    odata_id = resource_data.get("@odata.id") or ""
    match = _ONLINE_MEETING_RECORDING_RE.match(odata_id)
    if match:
        meeting_id = match.group("meeting_id")
        recording_id = match.group("recording_id")
    else:
        # Best-effort fallback so a shape we don't fully recognise still
        # gets a stable idempotency key instead of being dropped.
        meeting_id = notification.subscription_id
        recording_id = notification.resource_data_id or notification.subscription_id
    return UCWebhookEvent(
        provider=TEAMS_PROVIDER,
        external_call_id=meeting_id,
        recording_id=recording_id,
        recording_url=None,
        raw={"graph_notification": notification.raw, "odata_id": odata_id},
    )


async def ingest_notification(
    db: AsyncSession, notification: ChangeNotification
) -> Dict[str, Any]:
    """Persist one already-validated Graph change notification.

    Returns a small ``{"action": ...}`` dict describing what happened —
    used by the router to build the response summary and by tests to
    assert behaviour without re-querying the DB.
    """
    integ = await resolve_teams_integration(db, aad_tenant_id=notification.tenant_id)
    if integ is None:
        logger.info(
            "teams_recording.notification.unknown_tenant",
            extra={
                "aad_tenant_id": notification.tenant_id,
                "subscription_id": notification.subscription_id,
            },
        )
        return {"action": "skipped_unknown_tenant"}

    await bind_tenant_async(db, integ.tenant_id)

    if _CALL_RECORD_RESOURCE_RE.match(notification.resource):
        record = await _upsert_call_record(
            db, tenant_id=integ.tenant_id, call_id=_call_id_of(notification)
        )
        return {"action": "call_record_upserted", "call_id": record.call_id}

    if notification.resource == RECORDING_RESOURCE:
        job = await _upsert_recording_job(
            db, tenant_id=integ.tenant_id, integration_id=integ.id, notification=notification
        )
        return {"action": "recording_job_upserted", "job_id": str(job.id)}

    logger.info(
        "teams_recording.notification.unhandled_resource",
        extra={"resource": notification.resource},
    )
    return {"action": "ignored_unknown_resource"}


def _call_id_of(notification: ChangeNotification) -> str:
    match = _CALL_RECORD_RESOURCE_RE.match(notification.resource)
    if match:
        return match.group("call_id")
    return notification.resource_data_id or notification.resource


async def _upsert_call_record(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    call_id: str,
    organizer: Optional[str] = None,
    join_url: Optional[str] = None,
) -> TeamsCallRecord:
    """Upsert the observed-call control-plane row.

    ``certification_status`` reuses the three values the migration's
    CHECK constraint allows (``scaffold`` / ``bot_required`` /
    ``recording_fetched``) — no schema change. We observed a call via a
    ``callRecords`` notification, which only fires for calls that
    happened; without a deployed media bot nothing could have recorded
    it, so that's ``bot_required``. Once a real bot is registered the
    stub swap means this branch stops firing for calls it actually
    captured (those show up via the bot callback / recording resource
    paths instead), so ``bot_required`` stays an accurate signal.
    """
    from backend.app.services.teams_recording.bot_interface import get_media_bot

    existing = (
        await db.execute(
            select(TeamsCallRecord).where(
                TeamsCallRecord.tenant_id == tenant_id,
                TeamsCallRecord.call_id == call_id,
            )
        )
    ).scalar_one_or_none()

    bot = get_media_bot()
    status = "scaffold" if bot.is_available() else "bot_required"

    if existing is not None:
        existing.certification_status = status
        if organizer:
            existing.organizer = organizer
        if join_url:
            existing.join_url = join_url
        await db.flush()
        await db.commit()
        return existing

    record = TeamsCallRecord(
        tenant_id=tenant_id,
        call_id=call_id,
        organizer=organizer,
        join_url=join_url,
        certification_status=status,
    )
    db.add(record)
    await db.flush()
    await db.commit()
    return record


async def _upsert_recording_job(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    integration_id: uuid.UUID,
    notification: ChangeNotification,
) -> UcRecordingJob:
    """Idempotent upsert keyed on (provider, external_call_id) —
    identical shape to ``api/uc_telephony.py``'s ``_upsert_job_and_dispatch``,
    duplicated rather than imported because that helper is coupled to the
    vendor webhook request/response cycle (it commits + is monkeypatched
    by name in UC tests); this is the Graph-notification-batch analogue.
    """
    event = notification_to_uc_event(notification)

    existing = (
        await db.execute(
            select(UcRecordingJob).where(
                UcRecordingJob.provider == TEAMS_PROVIDER,
                UcRecordingJob.external_call_id == event.external_call_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.payload = event.raw
        await db.flush()
        await db.commit()
        if existing.state in ("done", "dispatched", "in_progress"):
            return existing
        _enqueue_fetch(existing.id)
        return existing

    job = UcRecordingJob(
        tenant_id=tenant_id,
        integration_id=integration_id,
        provider=TEAMS_PROVIDER,
        external_call_id=event.external_call_id,
        recording_id=event.recording_id,
        recording_url=event.recording_url,
        payload=event.raw,
        state="pending",
        attempts=0,
    )
    db.add(job)
    await db.flush()
    await db.commit()
    _enqueue_fetch(job.id)
    return job


def _enqueue_fetch(job_id: uuid.UUID) -> None:
    """Fire-and-forget Celery dispatch — same suppression-on-failure
    posture as ``api/uc_telephony.py._enqueue_fetch`` so unit tests and
    a Celery-less environment don't fail the HTTP request."""
    try:
        from backend.app.services.telephony.uc.fetch_task import fetch_uc_recording

        fetch_uc_recording.delay(str(job_id))
    except Exception:
        logger.exception("fetch_uc_recording enqueue failed for teams job %s", job_id)


# ── Media-bot callback ingestion ────────────────────────────────────────


async def ingest_bot_event(db: AsyncSession, event: BotCallbackEvent) -> Dict[str, Any]:
    """Persist one validated media-bot callback event.

    ``session.started`` / ``session.stopped`` — no audio yet, so we only
    upsert the ``TeamsCallRecord`` control-plane row ("persist and mark
    for batch"). ``audio.available`` bridges straight into the live
    pipeline (see module docstring) because the bot already tells us
    exactly where the audio lives.
    """
    integ = await resolve_teams_integration(db, aad_tenant_id=event.aad_tenant_id)
    if integ is None:
        logger.info(
            "teams_recording.bot_callback.unknown_tenant",
            extra={"aad_tenant_id": event.aad_tenant_id, "call_id": event.call_id},
        )
        return {"action": "skipped_unknown_tenant"}

    await bind_tenant_async(db, integ.tenant_id)

    if event.event in (SESSION_STARTED, SESSION_STOPPED):
        record = await _upsert_call_record(
            db,
            tenant_id=integ.tenant_id,
            call_id=event.call_id,
            organizer=event.raw.get("organizer"),
            join_url=event.raw.get("join_url"),
        )
        return {"action": "call_record_upserted", "call_id": record.call_id}

    assert event.event == AUDIO_AVAILABLE  # enforced by parse_bot_callback
    interaction_id = await _create_interaction_from_bot_audio(
        db, tenant_id=integ.tenant_id, event=event
    )
    return {"action": "interaction_created", "interaction_id": str(interaction_id)}


async def _create_interaction_from_bot_audio(
    db: AsyncSession, *, tenant_id: uuid.UUID, event: BotCallbackEvent
) -> uuid.UUID:
    interaction = Interaction(
        tenant_id=tenant_id,
        channel="voice",
        source=TEAMS_PROVIDER,
        direction=event.raw.get("direction"),
        title=f"Teams compliance recording {event.call_id}",
        caller_phone=event.raw.get("caller_upn"),
        engine="deepgram",
        status="processing",
        duration_seconds=event.duration_seconds,
        thread_id=event.call_id,
        audio_url=event.audio_url,
    )
    db.add(interaction)
    await db.flush()

    existing = (
        await db.execute(
            select(TeamsCallRecord).where(
                TeamsCallRecord.tenant_id == tenant_id,
                TeamsCallRecord.call_id == event.call_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.recording_url = event.audio_url
        existing.certification_status = "recording_fetched"

    await db.commit()

    try:
        from backend.app.tasks import process_voice_interaction

        process_voice_interaction.delay(str(interaction.id))
    except Exception:
        logger.exception(
            "process_voice_interaction dispatch failed for Teams bot audio, interaction %s",
            interaction.id,
        )

    return interaction.id


__all__ = [
    "TEAMS_PROVIDER",
    "RECORDING_RESOURCE",
    "resolve_teams_integration",
    "notification_to_uc_event",
    "ingest_notification",
    "ingest_bot_event",
]
