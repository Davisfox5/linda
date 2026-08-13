"""Outbound webhook dispatcher.

Two-stage delivery:

1. ``emit_event(tenant_id, event, payload)`` — synchronously writes a
   ``WebhookDelivery`` row per matching Webhook (``pending`` status) and
   kicks off the Celery delivery task for each. The hot path doesn't
   block on HTTP, so callers (API handlers, Celery tasks) stay cheap.
2. ``deliver_one(delivery_id)`` (invoked from Celery) performs the actual
   HTTP POST, HMAC-signs the payload with the webhook's secret, and
   updates the delivery row with status + next_retry_at on failure.

Retry policy: 5 attempts with exponential backoff (10s, 1m, 5m, 30m, 2h).
After the 5th failure the row is marked ``dead_letter`` and the webhook's
``consecutive_failures`` counter is incremented for the admin UI.

Signature (replay-protected, see ``docs/webhooks.md``):
``X-Linda-Timestamp: <unix seconds>`` plus
``X-Linda-Signature-V2: t=<unix seconds>,v1=<hex>`` where the hex is
``HMAC_SHA256(secret, f"{timestamp}.{body}")``. Consumers recompute the
HMAC from the header timestamp + raw body and reject deliveries whose
timestamp is more than ±5 minutes from their clock. (The legacy
body-only ``X-Linda-Signature`` header was removed after the v2
migration window — it verified forever, i.e. was replayable.)

Event filtering: each Webhook row's ``events`` list either contains
``"*"`` (receive everything) or an explicit list of event names. We
match both exact names and (for fan-out convenience) the ``prefix.*``
form, so a webhook subscribing to ``"customer.*"`` receives all
customer.* events.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Webhook, WebhookDelivery
from backend.app.services.token_crypto import decrypt_token

logger = logging.getLogger(__name__)


def is_safe_webhook_url(url: str) -> bool:
    """Reject webhook URLs that point at internal infrastructure.

    Targets the SSRF surface where a tenant can register a delivery URL
    that resolves inside our network — loopback (127.x, ::1), link-local
    (169.254.x — AWS/GCP IMDS), or RFC1918 private ranges.

    The check is best-effort string-based: an attacker who controls a
    public DNS name pointing at a private IP will still pass here. The
    delivery transport itself is the line of defence in depth; this
    function blocks the *obvious* cases at submit time.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip()
    if not host:
        return False
    lowered = host.lower()
    # Catch the obvious named loopbacks before falling through to the
    # IP-literal check.
    if lowered in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return False
    if lowered.endswith(".localhost"):
        return False
    # Try to interpret the host as a literal IP. Only IP literals get
    # the private-range / loopback check — DNS lookups would slow down
    # webhook creation, so we accept the risk that a hostile DNS entry
    # masks an internal IP.
    try:
        ip = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        return True
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return True


# Backoff schedule in seconds — indexed by current attempt count (0-based).
_BACKOFF_SECONDS: List[int] = [10, 60, 300, 1800, 7200]
_MAX_ATTEMPTS = len(_BACKOFF_SECONDS)

# Delay between flushing a delivery row and the first delivery attempt.
# The row is only flushed (not committed) when the task is enqueued — the
# caller's transaction commits later — so an immediate task can beat the
# commit, find no row, and no-op as "missing" with nothing to retry it
# (the exact failure that parked every prod delivery in ``pending``).
# 10s comfortably clears any caller's commit; webhook_pending_sweep
# covers the pathological cases.
_DISPATCH_ENQUEUE_DELAY_S = 10

# Auto-disable threshold. When a single webhook racks up this many
# consecutive_failures (across all its deliveries) we flip
# ``Webhook.active = False`` and short-circuit every pending delivery
# to that URL to dead_letter — otherwise a single permanently-broken
# customer endpoint can park hundreds of retry tasks in the queue.
_CIRCUIT_BREAKER_THRESHOLD = 10


def sign_payload(payload: str, secret: str) -> str:
    """HMAC-SHA256 hex digest of ``payload`` using ``secret``."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_payload_v2(payload: str, secret: str, timestamp: int) -> str:
    """Replay-protected signature: HMAC over ``f"{timestamp}.{payload}"``.

    The timestamp travels in ``X-Linda-Timestamp`` (and inside the
    ``X-Linda-Signature-V2`` header as ``t=``), so a captured delivery
    replayed later fails verification on any consumer enforcing the
    documented ±5 minute tolerance.
    """
    return sign_payload(f"{timestamp}.{payload}", secret)


def _payload_shape_error(event: Any, payload: Any) -> Optional[str]:
    """Validate the enqueue-time contract: the delivered JSON body is an
    object with a string ``event`` and an object ``data``.

    ``_envelope`` builds the outer object itself, so the only ways to
    violate the contract are a non-string/empty event name or a
    non-dict payload. Returns a reason string when invalid, else None.
    """
    if not isinstance(event, str) or not event.strip():
        return f"event must be a non-empty string, got {type(event).__name__}"
    if not isinstance(payload, dict):
        return f"payload must be a JSON object, got {type(payload).__name__}"
    return None


def _event_matches(filters: List[str], event: str) -> bool:
    """Match an event name against a webhook's ``events`` list.

    - ``"*"`` matches anything.
    - ``"customer.*"`` matches ``customer.<anything>``.
    - Otherwise an exact string match.
    """
    if not filters:
        return False
    if "*" in filters:
        return True
    if event in filters:
        return True
    for f in filters:
        if f.endswith(".*"):
            prefix = f[:-2]
            if event.startswith(prefix + "."):
                return True
    return False


def dispatch_sync(
    db: Any,
    tenant_id: uuid.UUID,
    event: str,
    payload: Dict[str, Any],
    *,
    dispatch_now: bool = True,
) -> List[WebhookDelivery]:
    """Synchronous analogue of :func:`emit_event` for Celery workers.

    Celery tasks own a synchronous SQLAlchemy ``Session`` (see
    ``backend.app.tasks._get_sync_session``); they cannot await the async
    ``emit_event``. This mirrors that function exactly: write a
    ``WebhookDelivery`` row per matching active Webhook, then enqueue the
    Celery delivery task for each so the HTTP POST happens out-of-band.

    The callers wrap this in ``try/except Exception`` and treat any
    failure as non-fatal, so even if no webhooks are configured (the
    common case in fresh tenants) this is a fast no-op.
    """
    shape_error = _payload_shape_error(event, payload)
    if shape_error is not None:
        logger.error(
            "webhook payload rejected at enqueue (tenant=%s event=%r): %s",
            tenant_id, event, shape_error,
        )
        return []

    from sqlalchemy.orm import Session as _SyncSession  # local import to avoid cycle

    assert isinstance(db, _SyncSession), "dispatch_sync requires a sync SQLAlchemy Session"

    webhooks = list(
        db.query(Webhook)
        .filter(Webhook.tenant_id == tenant_id, Webhook.active.is_(True))
        .all()
    )

    deliveries: List[WebhookDelivery] = []

    for wh in webhooks:
        if not _event_matches(list(wh.events or []), event):
            continue
        # Mint the id up front so the envelope can carry it. Letting the
        # column default assign it at flush would mean rewriting the
        # payload afterwards, and the signature is computed over the
        # stored payload — one value, written once, is the safer shape.
        delivery_id = uuid.uuid4()
        delivery = WebhookDelivery(
            id=delivery_id,
            webhook_id=wh.id,
            tenant_id=tenant_id,
            event=event,
            payload=_envelope(event, tenant_id, payload, event_id=delivery_id),
            status="pending",
            attempts=[],
            attempt_count=0,
        )
        db.add(delivery)
        deliveries.append(delivery)

    if deliveries:
        db.flush()

    if dispatch_now:
        for d in deliveries:
            try:
                from backend.app.tasks import webhook_deliver

                webhook_deliver.apply_async(
                    args=[str(d.id), str(tenant_id)],
                    countdown=_DISPATCH_ENQUEUE_DELAY_S,
                )
            except Exception:
                # Celery not available (local dev, tests) — the row is
                # still in the DB and webhook_pending_sweep picks it up.
                logger.debug(
                    "Failed to enqueue webhook_deliver for %s", d.id, exc_info=True
                )
    return deliveries


async def emit_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    event: str,
    payload: Dict[str, Any],
    *,
    dispatch_now: bool = True,
) -> List[WebhookDelivery]:
    """Write a ``WebhookDelivery`` row per matching active webhook.

    When ``dispatch_now`` is True (default), we also enqueue the Celery
    delivery task so the HTTP POST fires right away. Set False for bulk
    replay / tests where you want to control timing yourself.

    Safe to call in any request or task handler — DB writes only, no
    blocking HTTP. Returns the delivery rows so callers can log or react.

    Payloads that would violate the delivery contract (JSON object with
    a string ``event`` and object ``data``) are rejected at enqueue —
    logged at error level and no delivery rows written.
    """
    shape_error = _payload_shape_error(event, payload)
    if shape_error is not None:
        logger.error(
            "webhook payload rejected at enqueue (tenant=%s event=%r): %s",
            tenant_id, event, shape_error,
        )
        return []

    stmt = select(Webhook).where(
        Webhook.tenant_id == tenant_id,
        Webhook.active.is_(True),
    )
    webhooks = list((await db.execute(stmt)).scalars().all())

    deliveries: List[WebhookDelivery] = []

    for wh in webhooks:
        if not _event_matches(list(wh.events or []), event):
            continue
        # See dispatch_sync: the id is minted here so the envelope and the
        # row carry the same value without a post-flush rewrite.
        delivery_id = uuid.uuid4()
        delivery = WebhookDelivery(
            id=delivery_id,
            webhook_id=wh.id,
            tenant_id=tenant_id,
            event=event,
            payload=_envelope(event, tenant_id, payload, event_id=delivery_id),
            status="pending",
            attempts=[],
            attempt_count=0,
        )
        db.add(delivery)
        deliveries.append(delivery)

    if deliveries:
        await db.flush()

    if dispatch_now:
        for d in deliveries:
            try:
                from backend.app.tasks import webhook_deliver

                webhook_deliver.apply_async(
                    args=[str(d.id), str(tenant_id)],
                    countdown=_DISPATCH_ENQUEUE_DELAY_S,
                )
            except Exception:
                # Celery not available (local dev, tests) — the row is
                # still in the DB and webhook_pending_sweep picks it up.
                logger.debug(
                    "Failed to enqueue webhook_deliver for %s", d.id, exc_info=True
                )
    return deliveries


async def _maybe_trip_circuit_breaker(
    db: AsyncSession, webhook: Webhook, now: datetime
) -> bool:
    """If a webhook has accumulated enough consecutive failures, flip
    ``active=False`` and dead-letter every pending delivery to that URL
    so they don't park in the queue retrying forever.

    Returns ``True`` when the breaker actually flipped this call.
    """
    threshold = _CIRCUIT_BREAKER_THRESHOLD
    if (webhook.consecutive_failures or 0) < threshold:
        return False
    if not webhook.active:
        return False  # already tripped earlier
    webhook.active = False
    try:
        pending = (
            await db.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.webhook_id == webhook.id,
                    WebhookDelivery.status == "pending",
                )
            )
        ).scalars().all()
        for d in pending:
            d.status = "dead_letter"
            d.next_retry_at = None
            d.last_error = "circuit_breaker_open"
        logger.warning(
            "webhook circuit breaker tripped: webhook=%s consecutive_failures=%s "
            "dead-lettered=%d pending deliveries",
            webhook.id, webhook.consecutive_failures, len(pending),
        )
    except Exception:
        logger.exception(
            "Failed to dead-letter pending deliveries for tripped webhook %s",
            webhook.id,
        )
    return True


async def deliver_one(db: AsyncSession, delivery_id: uuid.UUID) -> Dict[str, Any]:
    """Attempt one HTTP delivery for a ``WebhookDelivery`` row.

    Updates the row with attempt metadata. Returns a small summary dict
    so the calling Celery task can log the outcome.
    """
    delivery = await db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return {"status": "missing", "id": str(delivery_id)}
    if delivery.status in ("sent", "dead_letter"):
        return {"status": delivery.status, "id": str(delivery_id)}

    webhook = await db.get(Webhook, delivery.webhook_id)
    if webhook is None or not webhook.active:
        delivery.status = "dead_letter"
        delivery.last_error = "webhook row missing or disabled"
        return {"status": "dead_letter", "id": str(delivery_id)}

    # Defence in depth: even though we validate at create/update time,
    # re-check before dispatch. A row inserted directly by SQL or via a
    # legacy API version could otherwise sneak past.
    if not is_safe_webhook_url(webhook.url):
        delivery.status = "dead_letter"
        delivery.last_error = "webhook URL points at a private network"
        return {"status": "dead_letter", "id": str(delivery_id)}

    now = datetime.now(timezone.utc)
    payload_str = json.dumps(delivery.payload, separators=(",", ":"), default=str)
    # ``Webhook.secret`` is stored Fernet-encrypted; decrypt at dispatch
    # time. ``decrypt_token`` returns legacy plaintext unchanged for any
    # rows that predate the encryption rollout.
    plaintext_secret = decrypt_token(webhook.secret) or webhook.secret
    # v2 (timestamped) signature only — the legacy body-only
    # X-Linda-Signature was dropped once known consumers verified v2;
    # it was replayable by design. See docs/webhooks.md for the
    # verification contract and the ±5 minute tolerance.
    timestamp = int(now.timestamp())
    signature_v2 = sign_payload_v2(payload_str, plaintext_secret, timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-Linda-Event": delivery.event,
        "X-Linda-Timestamp": str(timestamp),
        "X-Linda-Signature-V2": f"t={timestamp},v1={signature_v2}",
        "X-Linda-Delivery": str(delivery.id),
        "X-Linda-Attempt": str(delivery.attempt_count + 1),
    }
    attempts = list(delivery.attempts or [])
    status_code: Optional[int] = None
    error: Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                webhook.url,
                content=payload_str,
                headers=headers,
            )
        status_code = response.status_code
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    except Exception as exc:  # pragma: no cover — defensive
        error = f"{type(exc).__name__}: {exc}"[:500]

    delivery.attempt_count += 1
    delivery.last_status_code = status_code
    delivery.last_error = error
    attempts.append(
        {
            "at": now.isoformat(),
            "status_code": status_code,
            "error": error,
        }
    )
    delivery.attempts = attempts

    # 2xx = success. Anything else (or no status_code because the request
    # errored) counts as a failure for retry purposes.
    if status_code is not None and 200 <= status_code < 300:
        delivery.status = "sent"
        delivery.delivered_at = now
        delivery.next_retry_at = None
        webhook.last_delivered_at = now
        webhook.consecutive_failures = 0
        return {"status": "sent", "id": str(delivery.id), "status_code": status_code}

    # Failed attempt — decide retry or dead-letter.
    if delivery.attempt_count >= _MAX_ATTEMPTS:
        delivery.status = "dead_letter"
        delivery.next_retry_at = None
        webhook.last_failure_at = now
        webhook.consecutive_failures = (webhook.consecutive_failures or 0) + 1
        await _maybe_trip_circuit_breaker(db, webhook, now)
        return {
            "status": "dead_letter",
            "id": str(delivery.id),
            "status_code": status_code,
            "error": error,
        }

    # Schedule the next retry. Celery handles the actual sleep via countdown.
    next_delay = _BACKOFF_SECONDS[delivery.attempt_count]
    delivery.status = "pending"
    delivery.next_retry_at = now + timedelta(seconds=next_delay)
    webhook.last_failure_at = now
    webhook.consecutive_failures = (webhook.consecutive_failures or 0) + 1
    tripped = await _maybe_trip_circuit_breaker(db, webhook, now)
    if tripped:
        # Circuit just opened — this delivery is dead-lettered along
        # with every other pending delivery for this URL. No retry
        # task gets enqueued.
        delivery.status = "dead_letter"
        delivery.next_retry_at = None
        return {
            "status": "dead_letter",
            "id": str(delivery.id),
            "status_code": status_code,
            "error": "circuit_breaker_open",
        }

    # Kick the retry task now; the caller (Celery) may also do this itself.
    try:
        from backend.app.tasks import webhook_deliver

        webhook_deliver.apply_async(
            args=[str(delivery.id), str(delivery.tenant_id)],
            countdown=next_delay,
        )
    except Exception:
        logger.debug("Failed to schedule webhook retry", exc_info=True)

    return {
        "status": "retrying",
        "id": str(delivery.id),
        "status_code": status_code,
        "error": error,
        "next_retry_in": next_delay,
    }


def _envelope(
    event: str,
    tenant_id: uuid.UUID,
    payload: Dict[str, Any],
    event_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Wrap the caller-provided payload in a consistent outer envelope.

    Keeps receivers decoupled from whether we add fields like ``event``
    or ``tenant_id`` — they can read off the envelope.

    ``event_id`` is the delivery row's UUID, mirroring the
    ``X-Linda-Delivery`` header into the body so receivers that only see
    the parsed JSON (queue workers, proxies that strip headers) can still
    dedupe.  It is stable across retries of the same delivery, and it is
    per-delivery: one logical event fanned out to two endpoints carries
    two ids, which is correct for per-receiver dedupe but must not be
    used to correlate the same event across receivers.
    """
    body = {
        "event": event,
        "tenant_id": str(tenant_id),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }
    if event_id is not None:
        body["event_id"] = str(event_id)
    return body


# ── Backwards-compat class API used by the existing api/webhooks.py ──


class WebhookDispatcher:
    """Legacy class wrapper retained for the test-ping endpoint and other
    callers that invoked ``.dispatch()`` / ``.sign_payload()``. New code
    should use the module-level functions above instead."""

    async def dispatch(
        self,
        tenant_id,
        event: str,
        payload: dict,
        db: AsyncSession,
    ) -> None:
        """Enqueue a delivery for every active webhook subscribed to ``event``."""
        await emit_event(db, uuid.UUID(str(tenant_id)), event, payload)

    def sign_payload(self, payload: str, secret: str) -> str:
        return sign_payload(payload, secret)

    def sign_payload_v2(self, payload: str, secret: str, timestamp: int) -> str:
        return sign_payload_v2(payload, secret, timestamp)
