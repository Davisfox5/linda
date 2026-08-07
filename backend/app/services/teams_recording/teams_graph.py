"""Microsoft Graph subscription lifecycle for Teams compliance recording.

Three concerns, all built on top of the existing pieces in this
package rather than duplicating them:

* **Token acquisition** — delegates to
  ``services/teams_recording/graph_app_auth.GraphAppAuth`` (already
  does client-credentials + in-process caching). ``bearer_token`` below
  is a thin convenience wrapper so callers of this module don't need a
  second import.
* **Subscription create/renew/delete** — ``create_subscription`` already
  lived in ``subscriptions.py``; this module adds the renew (``PATCH``)
  and delete (``DELETE``) calls Graph requires to keep a subscription
  alive / tear it down, plus a customer-onboarding helper
  (``bootstrap_teams_integration``) that creates the ``Integration`` row
  + subscriptions together.
* **Renewal orchestration** — ``renew_due_teams_subscriptions`` is the
  function an integrator wires to Celery beat (see module-level note
  below; this module deliberately does NOT touch
  ``backend/app/tasks.py`` or ``main.py``).

Subscription bookkeeping (subscription id, resource, client_state,
expiration) is stored in ``Integration.provider_config["graph_subscriptions"]``
— a JSON list, one entry per subscribed resource — rather than a
dedicated table. ``CERTIFICATION_PATH.md`` originally flagged "DB,
renewal scheduler" as a follow-on; a new table would be a
``backend/app/models.py`` schema change, which is a sensitive-path edit
this workstream doesn't make. Reusing the existing ``provider_config``
JSONB column (the same column RingCentral/Webex/Zoom Phone integrations
already use for adapter-specific state) avoids that without leaving the
renewal problem unsolved.

── Celery-beat wiring the integrator must add (NOT done here) ──

This module never imports or edits ``backend/app/tasks.py``. To make
renewal actually run on a schedule, add a small Celery task there that
calls :func:`renew_due_teams_subscriptions`, e.g.::

    @celery_app.task(name="renew_teams_subscriptions")
    def renew_teams_subscriptions() -> Dict[str, Any]:
        import asyncio
        from backend.app.db import get_sessionmaker  # or _get_sync_session-style helper
        from backend.app.services.teams_recording.teams_graph import (
            renew_due_teams_subscriptions,
        )

        async def _run():
            async with get_sessionmaker()() as db:
                return await renew_due_teams_subscriptions(db)

        return asyncio.run(_run())

and a beat entry (Graph's shortest-lived subscribed resource —
``onlineMeetings/getAllRecordings`` — caps at ~60 minutes, so beat
should run more often than the ``within_minutes`` slack passed to
``renew_due_teams_subscriptions``, e.g. every 15 minutes against the
default ``within_minutes=15``)::

    "renew-teams-subscriptions": {
        "task": "renew_teams_subscriptions",
        "schedule": crontab(minute="*/15"),
    }

``tasks.py`` already runs an async DB call from inside a sync Celery
task elsewhere in this file (see ``_get_sync_session`` /
``asyncio.run`` usages in ``fetch_task.py`` and ``tasks.py`` itself) —
follow whichever of those two idioms ``tasks.py`` already uses for
async-session-from-sync-task, since that idiom lives in the file this
module is not allowed to touch.
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.app.config import get_settings
from backend.app.models import Integration
from backend.app.services.teams_recording.graph_app_auth import (
    GraphAppAuth,
    GraphAppAuthError,
    get_graph_app_auth,
)
from backend.app.services.teams_recording.subscriptions import (
    SUPPORTED_RESOURCES,
    CreateSubscriptionResult,
    SubscriptionSpec,
    SubscriptionValidationError,
    _parse_iso8601,
    create_subscription,
)
from backend.app.tenant_ctx import bind_tenant_async

logger = logging.getLogger(__name__)

TEAMS_INTEGRATION_PROVIDER = "teams_compliance"
_SUBSCRIPTIONS_KEY = "graph_subscriptions"
# How far ahead of expiry a subscription is considered "due" for
# renewal. Graph's shortest-lived resource we subscribe to
# (onlineMeetings/getAllRecordings) caps at ~60 minutes; this default
# assumes the integrator's Celery beat entry runs at least this often.
_DEFAULT_RENEW_SLACK_MINUTES = 15
# Placeholder value stored (encrypted) in Integration.access_token so
# the shared fetch_uc_recording task's "must have a decrypted token"
# guard passes. teams_compliance.fetch_recording never reads it — see
# services/telephony/uc/teams_compliance.py's module docstring.
_PLACEHOLDER_ACCESS_TOKEN = "app-only-graph-auth-see-teams_graph.py"


def bearer_token(auth: Optional[GraphAppAuth] = None) -> str:
    """Convenience wrapper around ``GraphAppAuth.authorization_header``.

    Kept here (rather than requiring callers to import
    ``graph_app_auth`` directly) so this module is the one-stop entry
    point for "everything the subscription lifecycle needs".
    """
    return (auth or get_graph_app_auth()).authorization_header()


def notification_url(*, base_url: Optional[str] = None) -> str:
    """Build the public HTTPS URL Graph should POST change notifications
    to: ``{PUBLIC_WEBHOOK_BASE_URL}{API_V1_PREFIX}/teams/notification``.

    Matches the example in ``USER_TODO.md``
    (``https://api.linda.example.com/api/v1/teams/notification``).
    """
    settings = get_settings()
    root = (base_url or settings.PUBLIC_WEBHOOK_BASE_URL or "").rstrip("/")
    if not root:
        raise SubscriptionValidationError(
            "PUBLIC_WEBHOOK_BASE_URL is not configured; cannot derive the "
            "Teams notification URL for subscription create/renew."
        )
    return f"{root}{settings.API_V1_PREFIX}/teams/notification"


# ── Renew / delete (create_subscription already lives in subscriptions.py) ──


@dataclass
class RenewSubscriptionResult:
    subscription_id: str
    expiration: datetime
    raw: Dict[str, Any]


async def renew_subscription(
    subscription_id: str,
    *,
    lifetime_minutes: Optional[int] = None,
    auth: Optional[GraphAppAuth] = None,
    http_client: Any = None,
) -> RenewSubscriptionResult:
    """``PATCH /v1.0/subscriptions/{id}`` with a fresh
    ``expirationDateTime``. Graph keeps the same subscription id — the
    only mutable field on renewal is the expiry.
    """
    from backend.app.services.teams_recording.subscriptions import (
        _DEFAULT_LIFETIME_MIN,
    )

    auth = auth or get_graph_app_auth()
    minutes = lifetime_minutes if lifetime_minutes is not None else _DEFAULT_LIFETIME_MIN
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    body = {"expirationDateTime": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")}

    if http_client is None:  # pragma: no cover - exercised at integration time
        import httpx

        http_client = httpx.AsyncClient(timeout=30.0)
        owns_client = True
    else:
        owns_client = False

    try:
        try:
            header = auth.authorization_header()
        except GraphAppAuthError as exc:
            logger.error("teams_recording.subscription.renew_no_auth", exc_info=exc)
            raise

        response = await http_client.patch(
            f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}",
            json=body,
            headers={"Authorization": header, "Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise SubscriptionValidationError(
                f"Graph rejected subscription renew: {response.status_code} "
                f"{getattr(response, 'text', '')[:500]}"
            )
        data = response.json()
    finally:
        if owns_client:
            await http_client.aclose()

    expiration = _parse_iso8601(data.get("expirationDateTime"))
    return RenewSubscriptionResult(
        subscription_id=data.get("id", subscription_id), expiration=expiration, raw=data
    )


async def delete_subscription(
    subscription_id: str,
    *,
    auth: Optional[GraphAppAuth] = None,
    http_client: Any = None,
) -> None:
    """``DELETE /v1.0/subscriptions/{id}``. Graph returns 204; any 4xx
    other than 404 (already gone) is surfaced as
    :class:`SubscriptionValidationError`."""
    auth = auth or get_graph_app_auth()

    if http_client is None:  # pragma: no cover - exercised at integration time
        import httpx

        http_client = httpx.AsyncClient(timeout=30.0)
        owns_client = True
    else:
        owns_client = False

    try:
        try:
            header = auth.authorization_header()
        except GraphAppAuthError as exc:
            logger.error("teams_recording.subscription.delete_no_auth", exc_info=exc)
            raise

        response = await http_client.delete(
            f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}",
            headers={"Authorization": header},
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise SubscriptionValidationError(
                f"Graph rejected subscription delete: {response.status_code} "
                f"{getattr(response, 'text', '')[:500]}"
            )
    finally:
        if owns_client:
            await http_client.aclose()


# ── Customer onboarding (Integration row + subscriptions together) ──────


async def bootstrap_teams_integration(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    aad_tenant_id: str,
    resources: Sequence[str] = SUPPORTED_RESOURCES,
    notification_base_url: Optional[str] = None,
    auth: Optional[GraphAppAuth] = None,
    http_client: Any = None,
) -> Integration:
    """One-time per-customer bootstrap.

    Run this once a customer's Teams admin has completed the Azure AD
    consent + PowerShell steps in
    ``docs/integrations/stream-3-teams/DEPLOYMENT_RUNBOOK.md``. Creates
    (or refreshes) the ``teams_compliance`` ``Integration`` row for
    ``tenant_id`` and registers a Graph subscription for every resource
    in ``resources`` (default: both of ``SUPPORTED_RESOURCES``).

    Not wired to any HTTP route — this is deliberately just a function
    an admin script / one-off shell / future admin endpoint can call.
    Wiring an admin API route for it is out of scope for this round.
    """
    settings = get_settings()
    client_state = settings.TEAMS_GRAPH_CLIENT_STATE
    if not client_state:
        raise SubscriptionValidationError(
            "TEAMS_GRAPH_CLIENT_STATE is not configured; refusing to create "
            "Graph subscriptions with no clientState to validate inbound "
            "notifications against."
        )
    url = notification_url(base_url=notification_base_url)

    await bind_tenant_async(db, tenant_id)

    existing = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == TEAMS_INTEGRATION_PROVIDER,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        from backend.app.services.token_crypto import encrypt_token

        existing = Integration(
            tenant_id=tenant_id,
            provider=TEAMS_INTEGRATION_PROVIDER,
            access_token=encrypt_token(_PLACEHOLDER_ACCESS_TOKEN),
            provider_config={"aad_tenant_id": aad_tenant_id},
        )
        db.add(existing)
        await db.flush()
    else:
        existing.provider_config = {
            **(existing.provider_config or {}),
            "aad_tenant_id": aad_tenant_id,
        }

    subs_state: List[Dict[str, Any]] = []
    for resource in resources:
        spec = SubscriptionSpec(
            resource=resource, notification_url=url, client_state=client_state
        )
        result: CreateSubscriptionResult = await create_subscription(
            spec, auth=auth, http_client=http_client
        )
        subs_state.append(
            {
                "resource": resource,
                "subscription_id": result.subscription_id,
                "expiration": result.expiration.isoformat(),
                "client_state": result.client_state,
            }
        )

    existing.provider_config = {
        **(existing.provider_config or {}),
        "aad_tenant_id": aad_tenant_id,
        _SUBSCRIPTIONS_KEY: subs_state,
    }
    flag_modified(existing, "provider_config")  # belt-and-suspenders for JSONB
    await db.commit()
    return existing


# ── Renewal orchestration (wire this to Celery beat — see module docstring) ──


async def renew_due_teams_subscriptions(
    db: AsyncSession,
    *,
    within_minutes: int = _DEFAULT_RENEW_SLACK_MINUTES,
    auth: Optional[GraphAppAuth] = None,
    http_client: Any = None,
) -> List[Dict[str, Any]]:
    """Renew every Graph subscription recorded on a ``teams_compliance``
    ``Integration`` row that expires within ``within_minutes``.

    This is the "clearly-named async function for renewal" the
    Celery-beat wiring described in this module's docstring should
    call. It is intentionally plain async — no Celery import here, so
    it stays unit-testable without a broker and this module never has
    to touch ``backend/app/tasks.py``.
    """
    results: List[Dict[str, Any]] = []
    integrations = (
        await db.execute(
            select(Integration).where(Integration.provider == TEAMS_INTEGRATION_PROVIDER)
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=within_minutes)

    for integ in integrations:
        # Deep-copy before mutating: entries would otherwise be the SAME
        # dict objects still referenced by ``integ.provider_config``, so
        # mutating them in place before reassigning the column makes
        # SQLAlchemy's content-based history comparison see "no change"
        # and silently skip the UPDATE.
        subs = copy.deepcopy((integ.provider_config or {}).get(_SUBSCRIPTIONS_KEY) or [])
        changed = False
        for entry in subs:
            try:
                expiration = datetime.fromisoformat(entry["expiration"])
            except (KeyError, TypeError, ValueError):
                continue
            if expiration > deadline:
                continue
            subscription_id = entry.get("subscription_id")
            try:
                renewed = await renew_subscription(
                    subscription_id, auth=auth, http_client=http_client
                )
            except Exception as exc:  # noqa: BLE001 — one bad subscription must not abort the sweep
                logger.exception(
                    "teams_recording.subscription.renew_failed",
                    extra={"subscription_id": subscription_id, "integration_id": str(integ.id)},
                )
                results.append(
                    {
                        "integration_id": str(integ.id),
                        "subscription_id": subscription_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                continue
            entry["expiration"] = renewed.expiration.isoformat()
            changed = True
            results.append(
                {
                    "integration_id": str(integ.id),
                    "subscription_id": renewed.subscription_id,
                    "status": "renewed",
                    "expiration": entry["expiration"],
                }
            )
        if changed:
            integ.provider_config = {**(integ.provider_config or {}), _SUBSCRIPTIONS_KEY: subs}
            flag_modified(integ, "provider_config")  # belt-and-suspenders for JSONB

    if results:
        await db.commit()
    return results


__all__ = [
    "TEAMS_INTEGRATION_PROVIDER",
    "RenewSubscriptionResult",
    "bearer_token",
    "bootstrap_teams_integration",
    "delete_subscription",
    "notification_url",
    "renew_due_teams_subscriptions",
    "renew_subscription",
]
