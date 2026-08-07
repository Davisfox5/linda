"""UC recording-provider contract.

Mirrors the shape of MeetingProvider so the two adapter families look
familiar to anyone reading either.

Lifecycle:

* ``verify_webhook`` runs on every inbound HTTP request to
  ``/uc/{provider}/webhook``. Returns the parsed UCWebhookEvent on
  success or raises WebhookVerificationError on signature mismatch /
  replay / unknown event.
* ``fetch_recording`` runs inside the Celery ``fetch_uc_recording``
  task. Returns the audio bytes plus the AudioFormat hint that the
  audio normalizer needs.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from backend.app.services.audio import AudioFormat


class WebhookVerificationError(Exception):
    """Raised when a webhook fails authenticity checks.

    The route handler maps this to HTTP 401. Subtle distinction from
    HTTPException: this exception type travels through the provider
    adapters without dragging FastAPI imports into them, which keeps
    them unit-testable in isolation.
    """


@dataclass
class UCWebhookEvent:
    """Parsed, authenticated webhook payload."""

    provider: str
    external_call_id: str
    recording_id: str
    recording_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    started_at: Optional[str] = None
    direction: Optional[str] = None
    caller_phone: Optional[str] = None
    callee_phone: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchedRecording:
    """Output of ``fetch_recording`` — bytes + format hint."""

    audio_bytes: bytes
    content_type: str
    format_hint: Optional[AudioFormat] = None


class UCRecordingProvider(abc.ABC):
    """Implemented per UC vendor."""

    name: str = "abstract"

    @abc.abstractmethod
    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        signing_secret: str,
    ) -> UCWebhookEvent:
        """Authenticate + parse a webhook delivery."""

    @abc.abstractmethod
    async def fetch_recording(
        self,
        *,
        access_token: str,
        event: UCWebhookEvent,
    ) -> FetchedRecording:
        """Download the recording audio for ``event``."""


_PROVIDERS: Dict[str, "UCRecordingProvider"] = {}


def register(provider: "UCRecordingProvider") -> "UCRecordingProvider":
    """Decorator/registration hook for provider modules."""
    _PROVIDERS[provider.name] = provider
    return provider


def get_provider(name: str) -> "UCRecordingProvider":
    """Return the registered provider, importing the module on demand."""
    if name not in _PROVIDERS:
        if name == "ringcentral":
            from backend.app.services.telephony.uc import ringcentral  # noqa: F401
        elif name == "webex_calling":
            from backend.app.services.telephony.uc import webex  # noqa: F401
        elif name == "zoom_phone":
            from backend.app.services.telephony.uc import zoom_phone  # noqa: F401
        elif name == "teams_compliance":
            from backend.app.services.telephony.uc import teams_compliance  # noqa: F401
        else:
            raise KeyError(f"Unknown UC provider: {name!r}")
    return _PROVIDERS[name]


@dataclass
class TenantContext:
    """Resolved tenant context attached to a webhook delivery.

    The webhook signing secret is *not* on this struct: signing secrets
    are vendor-wide env vars (RINGCENTRAL_WEBHOOK_SECRET /
    WEBEX_WEBHOOK_SECRET / ZOOM_PHONE_WEBHOOK_SECRET), not per-tenant.
    See the docstring at the top of ``api/uc_telephony.py``.
    """

    tenant_id: uuid.UUID
    integration_id: uuid.UUID
    access_token: str
    refresh_token: Optional[str] = None
    provider_config: Dict[str, Any] = field(default_factory=dict)


__all__ = [
    "FetchedRecording",
    "TenantContext",
    "UCRecordingProvider",
    "UCWebhookEvent",
    "WebhookVerificationError",
    "get_provider",
    "register",
]
