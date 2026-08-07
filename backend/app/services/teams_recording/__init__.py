"""Microsoft Teams Compliance Recording — Python control plane.

This package owns the LINDA-side half of the Teams compliance recording
integration:

* App-only Microsoft Graph authentication (``graph_app_auth``) — separate
  from the user-OAuth registry in ``backend/app/api/oauth.py`` because
  the compliance bot acts as itself, not as a delegated user.
* Graph change-notification subscription parsing (``subscriptions``) and
  lifecycle (``teams_graph``) — create/renew/delete + per-customer
  bootstrap. Renewal *scheduling* (Celery beat) is the one piece left
  for an integrator to wire — see ``teams_graph.py``'s module docstring.
* Persistence (``ingest``) — turns parsed Graph notifications and
  media-bot callback events into ``TeamsCallRecord`` rows and
  ``UcRecordingJob`` rows that ride the existing UC fetch →
  transcription pipeline (``services/telephony/uc``).
* The versioned media-bot callback contract (``bot_callback``) — the
  receiving half of ``POST /teams/bot/callback``; the bot that would
  call it isn't built (see below).
* The abstract media-bot interface (``bot_interface``) plus a stub
  implementation that always reports "not deployed" until the .NET media
  bot is commissioned in a follow-on workstream.
* The customer-side PowerShell template helper (``policy``) for
  ``New-CsTeamsComplianceRecordingApplication``.

The actual media plane — joining a Teams call, mixing/muxing audio,
delivering frames to transcription — runs in a separate .NET stateful
media bot per Microsoft's ``Calls.AccessMedia.All`` certification
requirements. That bot, its Azure deployment, and Microsoft's
certification process are explicitly OUT OF SCOPE for this repo.

See ``docs/integrations/stream-3-teams/DEPLOYMENT_RUNBOOK.md`` for the
step-by-step rollout, ``CERTIFICATION_PATH.md`` for the architecture
rationale, and ``USER_TODO.md`` for the human-only checklist.
"""

from backend.app.services.teams_recording.bot_interface import (
    MediaBot,
    MediaBotStatus,
    StubMediaBot,
    get_media_bot,
)

__all__ = [
    "MediaBot",
    "MediaBotStatus",
    "StubMediaBot",
    "get_media_bot",
]
