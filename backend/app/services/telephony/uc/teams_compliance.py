"""Microsoft Teams compliance recording — UC-provider adapter.

Lets recordings observed via Graph change notifications ride the
*existing* UC fetch → transcription pipeline
(``services/telephony/uc/fetch_task.fetch_uc_recording``) instead of a
bespoke Teams-only Celery task. ``services/teams_recording/ingest.py``
upserts the ``UcRecordingJob`` rows (``provider="teams_compliance"``);
this module is what ``fetch_uc_recording`` calls to turn a job into
audio bytes.

Two differences from the RingCentral/Webex/Zoom Phone adapters this
mirrors:

* **Auth.** Teams compliance recording is app-only Graph
  (client-credentials against LINDA's own AAD app registration — see
  ``services/teams_recording/graph_app_auth.py``), not a per-tenant
  OAuth token. ``fetch_uc_recording`` still requires a non-empty
  decrypted ``Integration.access_token`` to run at all — the
  ``teams_compliance`` ``Integration`` row therefore carries a
  placeholder token (see
  ``services/teams_recording/teams_graph.bootstrap_teams_integration``)
  and :meth:`TeamsComplianceProvider.fetch_recording` ignores the
  ``access_token`` argument, minting a real app-only bearer via
  ``get_graph_app_auth()`` instead.
* **``verify_webhook`` is not on the HTTP ingress path.** Graph's
  change-notification envelope (validation handshake, batched entries)
  doesn't fit the single-event vendor-webhook shape
  ``UCRecordingProvider.verify_webhook`` was designed around — that
  parsing lives in ``services/teams_recording/subscriptions.py`` and
  ``services/teams_recording/ingest.py``. This method is still
  implemented (the abstract base requires it, and it's independently
  useful/testable) as a thin wrapper: given one already-serialised
  Graph notification batch, it re-parses it and projects the first
  recording-resource entry into the same ``UCWebhookEvent`` shape
  ``ingest.py`` builds, so both call paths share one conversion.
"""

from __future__ import annotations

import json
import logging
from typing import Mapping, Optional

import httpx

from backend.app.services.audio import AudioFormat
from backend.app.services.teams_recording.graph_app_auth import (
    GraphAppAuthError,
    get_graph_app_auth,
)
from backend.app.services.telephony.uc.base import (
    FetchedRecording,
    UCRecordingProvider,
    UCWebhookEvent,
    WebhookVerificationError,
    register,
)

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class TeamsComplianceProvider(UCRecordingProvider):
    """Adapter that lets Teams compliance recordings ride the shared UC
    fetch pipeline. See module docstring for the auth + verify_webhook
    caveats vs. the other UC vendors."""

    name = "teams_compliance"

    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        signing_secret: str,
    ) -> UCWebhookEvent:
        from backend.app.services.teams_recording.ingest import (
            RECORDING_RESOURCE,
            notification_to_uc_event,
        )
        from backend.app.services.teams_recording.subscriptions import (
            SubscriptionValidationError,
            parse_notifications,
        )

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookVerificationError(
                f"Teams notification body is not JSON: {exc}"
            ) from exc

        try:
            notifications = parse_notifications(
                payload, expected_client_state=signing_secret or None
            )
        except SubscriptionValidationError as exc:
            raise WebhookVerificationError(str(exc)) from exc

        recording_notes = [n for n in notifications if n.resource == RECORDING_RESOURCE]
        if not recording_notes:
            raise WebhookVerificationError(
                "No communications/onlineMeetings/getAllRecordings entries "
                "in the notification batch"
            )
        return notification_to_uc_event(recording_notes[0])

    async def fetch_recording(
        self,
        *,
        access_token: str,
        event: UCWebhookEvent,
    ) -> FetchedRecording:
        odata_id = (event.raw or {}).get("odata_id") or ""
        if not (event.recording_url or odata_id):
            raise WebhookVerificationError(
                "Teams recording event has neither recording_url nor "
                "Graph @odata.id; cannot build a content URL"
            )
        url = event.recording_url or f"{_GRAPH_BASE}/{odata_id}/content"

        try:
            bearer = get_graph_app_auth().authorization_header()
        except GraphAppAuthError as exc:
            raise WebhookVerificationError(
                f"Teams app-only Graph auth unavailable: {exc}"
            ) from exc

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(
                url, headers={"Authorization": bearer, "Accept": "audio/*"}
            )
            if resp.status_code >= 400:
                raise WebhookVerificationError(
                    f"Teams recording content fetch failed: {resp.status_code}"
                )
            content_type = (
                resp.headers.get("content-type") or "audio/mpeg"
            ).split(";")[0].strip()
            return FetchedRecording(
                audio_bytes=resp.content,
                content_type=content_type,
                format_hint=_format_hint(content_type),
            )


def _format_hint(content_type: str) -> Optional[AudioFormat]:
    ct = (content_type or "").lower()
    if "mpeg" in ct or "mp3" in ct:
        return AudioFormat.MP3
    if "wav" in ct or "wave" in ct:
        return AudioFormat.WAV
    return None


register(TeamsComplianceProvider())
