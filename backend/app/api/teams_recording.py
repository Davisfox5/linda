"""Microsoft Teams compliance recording — HTTP entry points.

Two endpoints:

* ``POST /teams/notification`` — receives Microsoft Graph change
  notifications. Handles the validation handshake (echo
  ``validationToken`` as plain text) and parses notification batches
  into :class:`ChangeNotification` objects, then persists them via
  ``services/teams_recording/ingest.py``: ``callRecords`` entries
  upsert a ``TeamsCallRecord`` row, ``onlineMeetings/getAllRecordings``
  entries upsert a ``UcRecordingJob`` (``provider="teams_compliance"``)
  and dispatch the existing ``fetch_uc_recording`` Celery task — the
  same pipeline RingCentral/Webex Calling/Zoom Phone recordings ride.
* ``POST /teams/bot/callback`` — versioned (``version: "1"``) contract
  for the (future) .NET media bot: ``session.started`` /
  ``session.stopped`` / ``audio.available`` events. Validated by a
  shared-secret header (``X-LINDA-Bot-Secret`` against
  ``TEAMS_BOT_CALLBACK_SECRET``) once that secret is configured; until
  then the endpoint stays in a lenient placeholder-acceptance mode so
  infrastructure (TLS, ingress, IP allowlists) can be validated ahead
  of the bot's real rollout. See
  ``services/teams_recording/bot_callback.py`` for the full contract
  and ``services/teams_recording/ingest.py`` for what happens to each
  event.

This router is mounted under the standard ``/api/v1`` prefix in
``main.py``. Both endpoints must be reachable from the public internet
(Microsoft's Graph IPs / the bot's Azure-hosted egress); CORS is
irrelevant for this surface.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.services.teams_recording import get_media_bot
from backend.app.services.teams_recording.bot_callback import (
    BotCallbackValidationError,
    parse_bot_callback,
)
from backend.app.services.teams_recording.bot_interface import MediaBotNotDeployedError
from backend.app.services.teams_recording.ingest import ingest_bot_event, ingest_notification
from backend.app.services.teams_recording.subscriptions import (
    SubscriptionValidationError,
    is_validation_handshake,
    parse_notifications,
    validation_response_body,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Graph change-notification webhook ────────────────────────────────


@router.post("/teams/notification", include_in_schema=True)
async def teams_notification(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    """Microsoft Graph change-notification receiver.

    Behaviour:

    * Validation handshake — when Graph creates a subscription it sends
      a one-shot ``POST`` with ``?validationToken=<random>``. We must
      reply 200 with the token echoed as ``text/plain`` within 10 s, or
      Graph refuses to register the subscription. Detected by
      :func:`is_validation_handshake`.
    * Notification batch — Graph posts ``{"value": [...]}`` for resource
      change events. We parse, validate ``clientState``, and persist
      each entry via ``ingest.ingest_notification`` (call-record
      metadata → ``TeamsCallRecord``; recording-bearing entries →
      ``UcRecordingJob`` + Celery dispatch). Returns 202 with a summary
      of what each entry resolved to.

    Authentication on this endpoint is the per-subscription
    ``clientState`` (``TEAMS_GRAPH_CLIENT_STATE``) that every entry in
    the batch must match — that's the documented Microsoft-recommended
    approach. When ``TEAMS_GRAPH_CLIENT_STATE`` is unset (no
    subscription bootstrapped yet), the check is skipped so the
    scaffold keeps parsing structurally valid batches during infra
    validation; nothing gets persisted until a ``teams_compliance``
    ``Integration`` row resolves the entry's tenant regardless. In a
    follow-on we'll also pin Microsoft's IP ranges at the ingress.
    """

    # Convert query params to a plain dict for the helper. ``request.query_params``
    # is a Multi*Dict; we only care about ``validationToken`` (single-valued).
    query = {key: value for key, value in request.query_params.items()}

    if is_validation_handshake(query):
        try:
            token = validation_response_body(query)
        except SubscriptionValidationError as exc:
            logger.warning(
                "teams_recording.notification.validation_bad_request",
                extra={"error": str(exc)},
            )
            return PlainTextResponse(
                str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        logger.info("teams_recording.notification.validation_ok")
        # Microsoft's docs require text/plain with the token verbatim.
        return PlainTextResponse(token, status_code=status.HTTP_200_OK)

    # Notification batch path. Body is JSON.
    try:
        body: Dict[str, Any] = await request.json()
    except Exception as exc:  # noqa: BLE001 — Graph could send anything; be defensive
        logger.warning(
            "teams_recording.notification.bad_json",
            extra={"error": repr(exc)},
        )
        return JSONResponse(
            {"error": "request body is not valid JSON"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    settings = get_settings()
    try:
        notifications = parse_notifications(
            body, expected_client_state=settings.TEAMS_GRAPH_CLIENT_STATE or None
        )
    except SubscriptionValidationError as exc:
        logger.warning(
            "teams_recording.notification.parse_failed",
            extra={"error": str(exc)},
        )
        return JSONResponse(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    logger.info(
        "teams_recording.notification.received",
        extra={
            "count": len(notifications),
            "resources": sorted({n.resource for n in notifications}),
        },
    )

    # Persist each entry independently — one tenant's DB hiccup or
    # not-yet-bootstrapped Integration must not fail the whole batch
    # (Graph would just retry the entire POST).
    results = []
    for note in notifications:
        try:
            results.append(await ingest_notification(db, note))
        except Exception:  # noqa: BLE001
            logger.exception(
                "teams_recording.notification.ingest_failed",
                extra={"subscription_id": note.subscription_id, "resource": note.resource},
            )
            results.append({"action": "error"})

    # Per Microsoft's spec, return 202 Accepted within 3 seconds.
    return JSONResponse(
        {"accepted": len(notifications), "results": results},
        status_code=status.HTTP_202_ACCEPTED,
    )


# ── .NET media bot callback ──────────────────────────────────────────


@router.post("/teams/bot/callback", include_in_schema=True)
async def teams_bot_callback(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    """Versioned callback contract for the (future) .NET media bot.

    See ``services/teams_recording/bot_callback.py`` for the full
    ``version: "1"`` envelope. Gating, in order:

    1. Media bot must be "deployed" (``get_media_bot().status()``) —
       today that's always false (:class:`StubMediaBot`), so this
       always 503s first. This keeps the scaffold honest: we never
       claim to be receiving real bot traffic.
    2. Once a real bot is registered: if ``TEAMS_BOT_CALLBACK_SECRET``
       is configured, the ``X-LINDA-Bot-Secret`` header must match it
       (401 on mismatch) and the payload must parse against the v1
       contract (400 on malformed payload) — persisted via
       ``ingest.ingest_bot_event``.
    3. If the secret isn't configured yet (bot deployed, but the
       integrator hasn't provisioned the callback secret), the endpoint
       stays lenient: recognised v1 payloads are still persisted, and
       anything else is accepted + logged without persistence — the
       same best-effort acceptance the pre-versioned placeholder had,
       so infra validation isn't blocked on secret provisioning order.
    """

    bot = get_media_bot()
    status_struct = bot.status()
    if not status_struct.deployed:
        logger.info(
            "teams_recording.bot_callback.not_deployed",
            extra={"reason": status_struct.reason},
        )
        return JSONResponse(
            {"deployed": False, "reason": status_struct.reason},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    settings = get_settings()
    expected_secret = settings.TEAMS_BOT_CALLBACK_SECRET
    if expected_secret:
        provided = request.headers.get("x-linda-bot-secret", "")
        if not hmac.compare_digest(provided, expected_secret):
            logger.warning("teams_recording.bot_callback.bad_secret")
            return JSONResponse(
                {"error": "invalid or missing X-LINDA-Bot-Secret"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

    try:
        bot.is_available()
    except MediaBotNotDeployedError as exc:
        return JSONResponse(
            {"deployed": False, "reason": str(exc)},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None

    if expected_secret:
        # Authenticated caller — hold it to the real contract.
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "request body is not valid JSON"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            event = parse_bot_callback(body)
        except BotCallbackValidationError as exc:
            logger.warning(
                "teams_recording.bot_callback.bad_payload", extra={"error": str(exc)}
            )
            return JSONResponse({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
        result = await ingest_bot_event(db, event)
        logger.info(
            "teams_recording.bot_callback.processed",
            extra={"event": event.event, "call_id": event.call_id, **result},
        )
        return JSONResponse(
            {"received": True, "event": event.event, **result},
            status_code=status.HTTP_200_OK,
        )

    # No secret configured yet — lenient placeholder mode.
    if isinstance(body, dict):
        try:
            event = parse_bot_callback(body)
        except BotCallbackValidationError:
            event = None
        if event is not None:
            result = await ingest_bot_event(db, event)
            logger.info(
                "teams_recording.bot_callback.processed_unauthenticated",
                extra={"event": event.event, "call_id": event.call_id, **result},
            )
            return JSONResponse(
                {"received": True, "event": event.event, **result},
                status_code=status.HTTP_200_OK,
            )

    logger.info("teams_recording.bot_callback.received", extra={"body": body})
    return JSONResponse({"received": True}, status_code=status.HTTP_200_OK)


__all__ = ["router"]
