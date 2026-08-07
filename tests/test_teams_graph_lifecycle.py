"""Tests for ``services/teams_recording/teams_graph.py`` — subscription
renew/delete, per-customer bootstrap, and renewal-sweep orchestration.

``create_subscription`` itself already has a documented "not exercised
against real Graph in CI" posture in ``subscriptions.py``; these tests
follow the same shape — every Graph HTTP call goes through a fake
``http_client`` (matching the ``auth``/``http_client`` injection points
``create_subscription`` was already built with) rather than a real
network call or even ``respx``, since ``renew``/``delete`` are simple
enough that a small fake response object is clearer than route mocking.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.teams_recording.graph_app_auth import GraphAppAuth, GraphToken

# Marked individually below (rather than module-wide) since the two
# notification_url tests are plain sync functions.


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json


class _FakeHttpClient:
    """Records every call; returns whatever the test queues up."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    async def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return self._responses.pop(0)

    async def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return self._responses.pop(0)

    async def aclose(self):
        pass


def _configured_auth() -> GraphAppAuth:
    auth = GraphAppAuth(client_id="cid", client_secret="csec", tenant_id="tid")
    fake_token = GraphToken(access_token="fixture-bearer", expires_at=time.time() + 1000, raw={})
    auth._cached = fake_token  # bypass MSAL entirely
    return auth


# ── notification_url ─────────────────────────────────────────────────


def test_notification_url_uses_public_webhook_base_and_api_prefix():
    from backend.app.services.teams_recording.teams_graph import notification_url

    url = notification_url(base_url="https://api.linda.example.com")
    assert url == "https://api.linda.example.com/api/v1/teams/notification"


def test_notification_url_raises_when_unconfigured(monkeypatch):
    from backend.app.config import get_settings
    from backend.app.services.teams_recording.subscriptions import SubscriptionValidationError
    from backend.app.services.teams_recording.teams_graph import notification_url

    get_settings.cache_clear()
    monkeypatch.setenv("PUBLIC_WEBHOOK_BASE_URL", "")
    try:
        with pytest.raises(SubscriptionValidationError, match="PUBLIC_WEBHOOK_BASE_URL"):
            notification_url()
    finally:
        get_settings.cache_clear()


# ── renew_subscription / delete_subscription ─────────────────────────


@pytest.mark.asyncio
async def test_renew_subscription_patches_and_returns_new_expiration():
    from backend.app.services.teams_recording.teams_graph import renew_subscription

    fake_client = _FakeHttpClient(
        [
            _FakeResponse(
                200,
                {"id": "sub-123", "expirationDateTime": "2026-05-07T16:50:00.0000000Z"},
            )
        ]
    )
    result = await renew_subscription(
        "sub-123", auth=_configured_auth(), http_client=fake_client
    )
    assert result.subscription_id == "sub-123"
    assert result.expiration.year == 2026
    method, url, kwargs = fake_client.calls[0]
    assert method == "PATCH"
    assert url == "https://graph.microsoft.com/v1.0/subscriptions/sub-123"
    assert kwargs["headers"]["Authorization"] == "Bearer fixture-bearer"


@pytest.mark.asyncio
async def test_renew_subscription_raises_on_graph_error():
    from backend.app.services.teams_recording.subscriptions import SubscriptionValidationError
    from backend.app.services.teams_recording.teams_graph import renew_subscription

    fake_client = _FakeHttpClient([_FakeResponse(404, text="Subscription not found")])
    with pytest.raises(SubscriptionValidationError, match="404"):
        await renew_subscription("gone", auth=_configured_auth(), http_client=fake_client)


@pytest.mark.asyncio
async def test_delete_subscription_sends_delete():
    from backend.app.services.teams_recording.teams_graph import delete_subscription

    fake_client = _FakeHttpClient([_FakeResponse(204)])
    await delete_subscription("sub-123", auth=_configured_auth(), http_client=fake_client)
    method, url, _ = fake_client.calls[0]
    assert method == "DELETE"
    assert url == "https://graph.microsoft.com/v1.0/subscriptions/sub-123"


@pytest.mark.asyncio
async def test_delete_subscription_tolerates_already_gone():
    from backend.app.services.teams_recording.teams_graph import delete_subscription

    fake_client = _FakeHttpClient([_FakeResponse(404)])
    # Must not raise — 404 means "already deleted", not an error.
    await delete_subscription("sub-123", auth=_configured_auth(), http_client=fake_client)


@pytest.mark.asyncio
async def test_delete_subscription_raises_on_real_error():
    from backend.app.services.teams_recording.subscriptions import SubscriptionValidationError
    from backend.app.services.teams_recording.teams_graph import delete_subscription

    fake_client = _FakeHttpClient([_FakeResponse(403, text="Forbidden")])
    with pytest.raises(SubscriptionValidationError, match="403"):
        await delete_subscription("sub-123", auth=_configured_auth(), http_client=fake_client)


# ── bootstrap_teams_integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_teams_integration_creates_integration_and_subscriptions(
    test_session_factory, test_tenant, monkeypatch
):
    from sqlalchemy import select

    from backend.app.config import get_settings
    from backend.app.models import Integration
    from backend.app.services.teams_recording.teams_graph import (
        TEAMS_INTEGRATION_PROVIDER,
        bootstrap_teams_integration,
    )

    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_GRAPH_CLIENT_STATE", "fixture-client-state")
    monkeypatch.setenv("PUBLIC_WEBHOOK_BASE_URL", "https://api.linda.example.com")
    try:
        fake_client = _FakeHttpClient(
            [
                _FakeResponse(
                    201,
                    {
                        "id": f"sub-{i}",
                        "expirationDateTime": "2026-05-07T16:50:00.0000000Z",
                    },
                )
                for i in range(2)  # one per SUPPORTED_RESOURCES entry
            ]
        )
        async with test_session_factory() as db:
            integ = await bootstrap_teams_integration(
                db,
                tenant_id=test_tenant.id,
                aad_tenant_id="customer-aad-tenant",
                auth=_configured_auth(),
                http_client=fake_client,
            )
        assert integ.provider == TEAMS_INTEGRATION_PROVIDER
        assert integ.provider_config["aad_tenant_id"] == "customer-aad-tenant"
        subs = integ.provider_config["graph_subscriptions"]
        assert len(subs) == 2
        assert {s["subscription_id"] for s in subs} == {"sub-0", "sub-1"}

        async with test_session_factory() as session:
            row = (
                await session.execute(
                    select(Integration).where(
                        Integration.tenant_id == test_tenant.id,
                        Integration.provider == TEAMS_INTEGRATION_PROVIDER,
                    )
                )
            ).scalar_one()
            assert row.access_token  # placeholder, but must be non-empty/encrypted
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_bootstrap_teams_integration_requires_client_state(
    test_session_factory, test_tenant, monkeypatch
):
    from backend.app.config import get_settings
    from backend.app.services.teams_recording.subscriptions import SubscriptionValidationError
    from backend.app.services.teams_recording.teams_graph import bootstrap_teams_integration

    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_GRAPH_CLIENT_STATE", "")
    try:
        async with test_session_factory() as db:
            with pytest.raises(SubscriptionValidationError, match="TEAMS_GRAPH_CLIENT_STATE"):
                await bootstrap_teams_integration(
                    db, tenant_id=test_tenant.id, aad_tenant_id="customer-aad-tenant"
                )
    finally:
        get_settings.cache_clear()


# ── renew_due_teams_subscriptions ─────────────────────────────────────


@pytest.mark.asyncio
async def test_renew_due_teams_subscriptions_renews_only_expiring_entries(
    test_session_factory, test_tenant
):
    from sqlalchemy import select

    from backend.app.models import Integration
    from backend.app.services.teams_recording.teams_graph import (
        TEAMS_INTEGRATION_PROVIDER,
        renew_due_teams_subscriptions,
    )
    from backend.app.services.token_crypto import encrypt_token

    now = datetime.now(timezone.utc)
    async with test_session_factory() as session:
        integ = Integration(
            tenant_id=test_tenant.id,
            provider=TEAMS_INTEGRATION_PROVIDER,
            access_token=encrypt_token("placeholder"),
            provider_config={
                "aad_tenant_id": "customer-aad-tenant",
                "graph_subscriptions": [
                    {
                        "resource": "communications/callRecords",
                        "subscription_id": "sub-expiring-soon",
                        "expiration": (now + timedelta(minutes=5)).isoformat(),
                        "client_state": "cs",
                    },
                    {
                        "resource": "communications/onlineMeetings/getAllRecordings",
                        "subscription_id": "sub-not-due-yet",
                        "expiration": (now + timedelta(hours=6)).isoformat(),
                        "client_state": "cs",
                    },
                ],
            },
        )
        session.add(integ)
        await session.commit()
        integ_id = integ.id

    fake_client = _FakeHttpClient(
        [
            _FakeResponse(
                200,
                {"id": "sub-expiring-soon", "expirationDateTime": "2026-06-01T00:00:00.0000000Z"},
            )
        ]
    )

    async with test_session_factory() as db:
        results = await renew_due_teams_subscriptions(
            db, within_minutes=15, auth=_configured_auth(), http_client=fake_client
        )

    assert len(results) == 1
    assert results[0]["subscription_id"] == "sub-expiring-soon"
    assert results[0]["status"] == "renewed"
    # Exactly one Graph call — the not-due subscription was left alone.
    assert len(fake_client.calls) == 1

    async with test_session_factory() as session:
        row = (
            await session.execute(select(Integration).where(Integration.id == integ_id))
        ).scalar_one()
        subs = {s["subscription_id"]: s for s in row.provider_config["graph_subscriptions"]}
        assert subs["sub-expiring-soon"]["expiration"].startswith("2026-06-01")
        assert subs["sub-not-due-yet"]["expiration"] == (now + timedelta(hours=6)).isoformat()


@pytest.mark.asyncio
async def test_renew_due_teams_subscriptions_records_failure_without_raising(
    test_session_factory, test_tenant
):
    from backend.app.models import Integration
    from backend.app.services.teams_recording.teams_graph import (
        TEAMS_INTEGRATION_PROVIDER,
        renew_due_teams_subscriptions,
    )
    from backend.app.services.token_crypto import encrypt_token

    now = datetime.now(timezone.utc)
    async with test_session_factory() as session:
        session.add(
            Integration(
                tenant_id=test_tenant.id,
                provider=TEAMS_INTEGRATION_PROVIDER,
                access_token=encrypt_token("placeholder"),
                provider_config={
                    "aad_tenant_id": "customer-aad-tenant",
                    "graph_subscriptions": [
                        {
                            "resource": "communications/callRecords",
                            "subscription_id": "sub-broken",
                            "expiration": (now - timedelta(minutes=1)).isoformat(),
                            "client_state": "cs",
                        }
                    ],
                },
            )
        )
        await session.commit()

    fake_client = _FakeHttpClient([_FakeResponse(500, text="server error")])
    async with test_session_factory() as db:
        results = await renew_due_teams_subscriptions(
            db, auth=_configured_auth(), http_client=fake_client
        )
    assert results[0]["status"] == "failed"
    assert results[0]["subscription_id"] == "sub-broken"
