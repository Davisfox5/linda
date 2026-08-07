"""Versioned contract for ``POST /teams/bot/callback``.

The (future) .NET media bot doesn't exist yet — Microsoft's Graph
Communications Calling SDK has no Python binding, so the actual media
plane is a separate, out-of-scope workstream (see
``docs/integrations/stream-3-teams/CERTIFICATION_PATH.md``). This
module defines the *receiving* half of the contract the bot will speak
once it's deployed: a small, versioned JSON envelope covering the three
lifecycle signals a compliance-recording bot needs to report.

Envelope (``version: "1"``)::

    {
      "version": "1",
      "event": "session.started" | "session.stopped" | "audio.available",
      "call_id": "<Graph call/meeting id the bot attached to>",
      "session_id": "<bot's own correlation id for this attach>",
      "aad_tenant_id": "<customer's Azure AD tenant id>",
      ... event-specific fields ...
    }

``aad_tenant_id`` is how a callback (which knows nothing about LINDA's
internal tenant ids) gets routed to the right customer — the same
identifier Graph itself puts on every change notification (see
``subscriptions.ChangeNotification.tenant_id``), resolved against the
``teams_compliance`` ``Integration`` row bootstrapped for that customer
(``services/teams_recording/teams_graph.bootstrap_teams_integration``).

Event-specific fields:

* ``session.started`` — ``organizer`` (UPN, optional), ``join_url``
  (optional), ``occurred_at`` (ISO-8601, optional).
* ``session.stopped`` — ``reason`` (optional), ``occurred_at``
  (optional).
* ``audio.available`` — ``audio_url`` (**required**, HTTPS — wherever
  the bot staged the recorded audio, e.g. an Azure Blob URL),
  ``duration_seconds`` (optional), ``direction`` (optional),
  ``caller_upn`` (optional).

This module only parses + validates the envelope shape; persistence
lives in ``services/teams_recording/ingest.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

SUPPORTED_VERSIONS = ("1",)

SESSION_STARTED = "session.started"
SESSION_STOPPED = "session.stopped"
AUDIO_AVAILABLE = "audio.available"

SUPPORTED_EVENTS = (SESSION_STARTED, SESSION_STOPPED, AUDIO_AVAILABLE)


class BotCallbackValidationError(ValueError):
    """Raised for a malformed/unsupported callback payload. The route
    handler converts this to a 400 (when a shared secret is
    configured — see api/teams_recording.py for the lenient
    not-yet-configured fallback)."""


@dataclass
class BotCallbackEvent:
    """Parsed, validated media-bot callback event."""

    version: str
    event: str
    call_id: str
    session_id: str
    aad_tenant_id: str
    raw: Dict[str, Any]

    @property
    def audio_url(self) -> Optional[str]:
        return self.raw.get("audio_url")

    @property
    def duration_seconds(self) -> Optional[int]:
        value = self.raw.get("duration_seconds")
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        return None


def parse_bot_callback(payload: Dict[str, Any]) -> BotCallbackEvent:
    """Validate + parse one callback payload.

    Raises :class:`BotCallbackValidationError` for anything that
    doesn't match the ``version: "1"`` envelope — an unrecognised
    ``event``, a missing correlation id, or a missing ``audio_url`` on
    an ``audio.available`` event.
    """
    if not isinstance(payload, dict):
        raise BotCallbackValidationError("callback payload is not a JSON object")

    version = str(payload.get("version") or "")
    if version not in SUPPORTED_VERSIONS:
        raise BotCallbackValidationError(
            f"unsupported callback version {version!r}; expected one of {SUPPORTED_VERSIONS}"
        )

    event = payload.get("event")
    if event not in SUPPORTED_EVENTS:
        raise BotCallbackValidationError(
            f"unknown event {event!r}; expected one of {SUPPORTED_EVENTS}"
        )

    call_id = payload.get("call_id")
    session_id = payload.get("session_id")
    aad_tenant_id = payload.get("aad_tenant_id")
    if not (call_id and session_id and aad_tenant_id):
        raise BotCallbackValidationError(
            "callback payload missing call_id/session_id/aad_tenant_id"
        )

    audio_url = payload.get("audio_url")
    if event == AUDIO_AVAILABLE:
        if not audio_url:
            raise BotCallbackValidationError("audio.available requires audio_url")
        if not str(audio_url).startswith("https://"):
            raise BotCallbackValidationError("audio_url must be HTTPS")
    elif audio_url is not None and not str(audio_url).startswith("https://"):
        raise BotCallbackValidationError("audio_url must be HTTPS")

    return BotCallbackEvent(
        version=version,
        event=event,
        call_id=str(call_id),
        session_id=str(session_id),
        aad_tenant_id=str(aad_tenant_id),
        raw=payload,
    )


__all__ = [
    "AUDIO_AVAILABLE",
    "SESSION_STARTED",
    "SESSION_STOPPED",
    "SUPPORTED_EVENTS",
    "SUPPORTED_VERSIONS",
    "BotCallbackEvent",
    "BotCallbackValidationError",
    "parse_bot_callback",
]
