"""Fanout layer for manager alerts: in-app + Slack + outbound webhooks.

Called inline after the anomaly detector inserts a ``ManagerAlert``
row. Channels in v1:

* **In-app** — one ``Notification`` row per recipient (managers +
  admins in the tenant), plus a Redis pub/sub publish so the SSE bell
  updates in real time.
* **Slack** — ``chat.postMessage`` against the tenant's installed
  Slack OAuth integration, subject to ``AlertChannelConfig`` severity
  gates.
* **Webhook** — one best-effort ``manager_alert.created`` enqueue per
  alert (anomaly scan, trend detector, commitment, concern, and
  campaign-monitor alerts alike), same idiom as
  ``services/outreach/replies.py``.

Email + push are deferred to a follow-up; the dispatch table here is
shaped to grow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable, List, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import (
    AlertChannelConfig,
    ManagerAlert,
    Notification,
    SlackIntegration,
    User,
)
from backend.app.services.notification_service import NotificationService
from backend.app.services.notifications import (
    NotificationKind,
    notification_channel,
    publish_notification,
)
from backend.app.services.token_crypto import decrypt_token
from backend.app.services.webhook_dispatcher import dispatch_sync

logger = logging.getLogger(__name__)


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def fanout(session: Session, alerts: Iterable[ManagerAlert]) -> None:
    """Deliver each alert to the configured channels. Best-effort.

    A failing channel logs but never raises — the alert row is already
    durable, so missed delivery is recoverable (the manager can still
    see it in-app and the next anomaly scan won't re-fire it).
    """
    alerts = [a for a in alerts if a is not None]
    if not alerts:
        return
    # Group by tenant so we only fetch each tenant's config + recipients once.
    by_tenant: dict = {}
    for alert in alerts:
        by_tenant.setdefault(alert.tenant_id, []).append(alert)

    for tenant_id, tenant_alerts in by_tenant.items():
        try:
            _fanout_tenant(session, tenant_id, tenant_alerts)
        except Exception:
            logger.exception(
                "Manager alert fanout failed for tenant %s (non-fatal)", tenant_id
            )


def _fanout_tenant(
    session: Session,
    tenant_id,
    alerts: Sequence[ManagerAlert],
) -> None:
    config = session.get(AlertChannelConfig, tenant_id)
    recipients = _manager_recipients(session, tenant_id)
    slack = session.execute(
        select(SlackIntegration).where(
            SlackIntegration.tenant_id == tenant_id,
            SlackIntegration.revoked_at.is_(None),
        )
    ).scalar_one_or_none()

    for alert in alerts:
        if not config or config.inapp_enabled:
            _deliver_inapp(session, alert, recipients)
        if _slack_enabled_for(alert, config, slack):
            _deliver_slack(slack, alert)
        _deliver_webhook(session, tenant_id, alert)


def _manager_recipients(session: Session, tenant_id) -> List[User]:
    """Managers + admins in the tenant get the in-app notification."""
    return (
        session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.role.in_(("manager", "admin")),
            )
        )
        .scalars()
        .all()
    )


def _deliver_inapp(
    session: Session, alert: ManagerAlert, recipients: Sequence[User]
) -> None:
    for user in recipients:
        try:
            row = Notification(
                tenant_id=alert.tenant_id,
                user_id=user.id,
                kind=NotificationKind.MANAGER_ALERT,
                title=alert.title[:200],
                body=alert.body,
                link_url=f"/manager?alert={alert.id}",
            )
            session.add(row)
            publish_notification(
                tenant_id=alert.tenant_id,
                user_id=user.id,
                payload={
                    "type": "notification",
                    "kind": NotificationKind.MANAGER_ALERT,
                    "title": alert.title[:200],
                    "alert_id": str(alert.id),
                    "severity": alert.severity,
                },
            )
        except Exception:
            logger.exception(
                "in-app notify failed for user %s alert %s (non-fatal)",
                user.id,
                alert.id,
            )


def _deliver_webhook(session: Session, tenant_id, alert: ManagerAlert) -> None:
    """Best-effort ``manager_alert.created`` enqueue — never fails the fanout.

    Same idiom as ``services/outreach/replies.py``: ``dispatch_sync`` inside
    a try/except, ``logger.warning`` on failure. ``campaign_id`` /
    ``campaign_name`` are lifted from ``alert.evidence`` when the
    campaign-monitor detector stamped them there; absent for every other
    alert kind.
    """
    evidence = alert.evidence or {}
    payload = {
        "alert_id": str(alert.id),
        "kind": alert.kind,
        "severity": alert.severity,
        "domain": alert.domain,
        "title": alert.title,
        "body": alert.body,
        "opened_at": alert.opened_at.isoformat() if alert.opened_at else None,
        "campaign_id": evidence.get("campaign_id"),
        "campaign_name": evidence.get("campaign_name"),
    }
    try:
        dispatch_sync(session, tenant_id, "manager_alert.created", payload)
    except Exception:
        logger.warning("manager_alert.created webhook enqueue failed", exc_info=True)


def _channel_for_alert(
    slack: SlackIntegration, alert: ManagerAlert
) -> str | None:
    """Pick the Slack channel id for an alert.

    Per-domain override map (``slack_integration.domain_channel_map``,
    added in ``dom_003``) wins when present; falls back to the
    tenant-wide ``default_channel_id``. Lets a tenant route Sales alerts
    to ``#sales-alerts`` and Support alerts to ``#support-alerts``
    without losing the legacy single-channel default.
    """
    domain_map = getattr(slack, "domain_channel_map", None) or {}
    if alert.domain and isinstance(domain_map, dict):
        override = domain_map.get(alert.domain)
        if override:
            return str(override)
    return slack.default_channel_id


def _slack_enabled_for(
    alert: ManagerAlert,
    config: AlertChannelConfig | None,
    slack: SlackIntegration | None,
) -> bool:
    if slack is None:
        return False
    # We're enabled as long as SOME channel can take the message —
    # either the per-domain override or the tenant default.
    if not _channel_for_alert(slack, alert):
        return False
    if config is None:
        return False
    if not config.slack_enabled:
        return False
    min_rank = _SEVERITY_RANK.get(config.slack_min_severity or "medium", 2)
    return _SEVERITY_RANK.get(alert.severity, 0) >= min_rank


def _deliver_slack(slack: SlackIntegration, alert: ManagerAlert) -> None:
    try:
        bot_token = decrypt_token(slack.bot_token_encrypted) or ""
    except Exception:
        logger.exception(
            "Slack bot token decrypt failed for tenant %s", slack.tenant_id
        )
        return
    channel = _channel_for_alert(slack, alert)
    if not bot_token or not channel:
        return
    blocks = _slack_blocks(alert)
    service = NotificationService()
    try:
        asyncio.run(
            service.post_to_slack_channel(
                bot_token=bot_token,
                channel_id=channel,
                text=alert.title,
                blocks=blocks,
            )
        )
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.create_task(
            service.post_to_slack_channel(
                bot_token=bot_token,
                channel_id=channel,
                text=alert.title,
                blocks=blocks,
            )
        )


def _slack_blocks(alert: ManagerAlert) -> list:
    severity_emoji = {"high": ":rotating_light:", "medium": ":warning:", "low": ":information_source:"}.get(
        alert.severity, ":bell:"
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{severity_emoji} *Manager alert:* {alert.title}",
            },
        },
        *(
            [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": alert.body},
                }
            ]
            if alert.body
            else []
        ),
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Severity: *{alert.severity}* · "
                        f"Kind: `{alert.kind}` · "
                        f"Evidence: `{json.dumps(alert.evidence, default=str)[:200]}`"
                    ),
                }
            ],
        },
    ]
