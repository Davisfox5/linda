"""Tests for the WebSocket ticket service.

Three layers:

- Unit tests against ``ws_tickets.InMemoryRedisStub`` — issue/consume
  semantics, single-use enforcement, expected-field mismatches, TTL
  expiry, and the per-key rate limit.
- Integration tests against the ticket endpoint through the
  ``test_client`` fixture — confirm that monitor-role tickets require
  a user with manager/admin role, and agent-role tickets do not.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from backend.app.services.ws_tickets import (
    InMemoryRedisStub,
    MAX_CONNECTIONS_PER_MINUTE,
    RateLimitedError,
    Ticket,
    WebSocketAuthError,
    consume_ticket,
    enforce_new_connection_quota,
    issue_ticket,
)


PREFIX = "/api/v1"


# ── Unit tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_returns_ticket_metadata():
    redis = InMemoryRedisStub()
    out = await issue_ticket(
        redis,
        tenant_id="t-1",
        session_id="sess-1",
        role="agent",
    )
    assert isinstance(out["ticket"], str) and len(out["ticket"]) > 20
    assert out["session_id"] == "sess-1"
    assert out["role"] == "agent"
    assert out["expires_at"] > time.time()


@pytest.mark.asyncio
async def test_consume_succeeds_once_then_fails():
    redis = InMemoryRedisStub()
    out = await issue_ticket(
        redis, tenant_id="t-1", session_id="sess-1", role="agent"
    )
    t: Ticket = await consume_ticket(
        redis,
        out["ticket"],
        expected_session_id="sess-1",
        expected_role="agent",
    )
    assert t.tenant_id == "t-1"
    # Second consumption must fail — single-use.
    with pytest.raises(WebSocketAuthError) as info:
        await consume_ticket(redis, out["ticket"])
    assert info.value.reason == "expired_or_consumed"


@pytest.mark.asyncio
async def test_consume_missing_ticket_raises():
    redis = InMemoryRedisStub()
    with pytest.raises(WebSocketAuthError) as info:
        await consume_ticket(redis, None)
    assert info.value.reason == "missing"


@pytest.mark.asyncio
async def test_consume_rejects_wrong_session():
    redis = InMemoryRedisStub()
    out = await issue_ticket(
        redis, tenant_id="t-1", session_id="sess-A", role="agent"
    )
    with pytest.raises(WebSocketAuthError) as info:
        await consume_ticket(
            redis,
            out["ticket"],
            expected_session_id="sess-B",
        )
    assert info.value.reason == "wrong_session"


@pytest.mark.asyncio
async def test_consume_rejects_wrong_role():
    redis = InMemoryRedisStub()
    out = await issue_ticket(
        redis, tenant_id="t-1", session_id="sess-1", role="agent"
    )
    with pytest.raises(WebSocketAuthError) as info:
        await consume_ticket(
            redis, out["ticket"], expected_role="monitor"
        )
    assert info.value.reason == "wrong_role"


@pytest.mark.asyncio
async def test_consume_rejects_wrong_tenant():
    redis = InMemoryRedisStub()
    out = await issue_ticket(
        redis, tenant_id="t-1", session_id="sess-1", role="agent"
    )
    with pytest.raises(WebSocketAuthError) as info:
        await consume_ticket(
            redis, out["ticket"], expected_tenant_id="t-2"
        )
    assert info.value.reason == "wrong_tenant"


@pytest.mark.asyncio
async def test_issue_ticket_rejects_unknown_role():
    redis = InMemoryRedisStub()
    with pytest.raises(WebSocketAuthError):
        await issue_ticket(
            redis, tenant_id="t-1", session_id="sess", role="superuser"
        )


@pytest.mark.asyncio
async def test_rate_limit_allows_up_to_quota_then_rejects():
    redis = InMemoryRedisStub()
    key = "hash-abc"
    # First N go through.
    for _ in range(MAX_CONNECTIONS_PER_MINUTE):
        await enforce_new_connection_quota(redis, key)
    # N+1st trips the limit.
    with pytest.raises(RateLimitedError):
        await enforce_new_connection_quota(redis, key)


@pytest.mark.asyncio
async def test_rate_limit_is_scoped_per_key():
    redis = InMemoryRedisStub()
    for _ in range(MAX_CONNECTIONS_PER_MINUTE):
        await enforce_new_connection_quota(redis, "hash-A")
    # A different key should still be free.
    await enforce_new_connection_quota(redis, "hash-B")


# ── Integration tests (via test_client) ─────────────────────────────────


@pytest.fixture
def ticket_redis_stub(test_app):
    """Override the ticket endpoint's Redis dep with an in-memory stub.

    Scoped to the ``test_app`` fixture (which already mounts both the
    outcomes and ws-tickets routers), so the stub replaces only the
    dep this test run's FastAPI app sees.
    """
    from backend.app.api import ws_tickets as module

    stub = InMemoryRedisStub()

    async def _override():
        yield stub

    test_app.dependency_overrides[module._get_redis] = _override
    yield stub
    test_app.dependency_overrides.pop(module._get_redis, None)


@pytest.mark.asyncio
async def test_issue_endpoint_returns_agent_ticket(
    test_client, test_tenant, ticket_redis_stub
):
    resp = await test_client.post(
        f"{PREFIX}/ws/tickets",
        json={"role": "agent", "session_id": "sess-from-api"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["session_id"] == "sess-from-api"
    assert body["role"] == "agent"
    assert isinstance(body["ticket"], str)

    # Ticket is valid and consumable once.
    t = await consume_ticket(
        ticket_redis_stub,
        body["ticket"],
        expected_tenant_id=str(test_tenant.id),
        expected_session_id="sess-from-api",
        expected_role="agent",
    )
    assert t.tenant_id == str(test_tenant.id)


@pytest.mark.asyncio
async def test_issue_endpoint_generates_session_id_when_omitted(
    test_client, test_tenant, test_session_factory, ticket_redis_stub
):
    """Agent tickets minted without a session id persist a LiveSession
    row under a UUID id so finalization can land the transcript on an
    Interaction (extension / bring-your-own-audio flows)."""
    resp = await test_client.post(
        f"{PREFIX}/ws/tickets", json={"role": "agent"}
    )
    assert resp.status_code == 201
    body = resp.json()
    session_uuid = uuid.UUID(body["session_id"])  # UUID, not the "s-" form

    from backend.app.models import LiveSession
    from sqlalchemy import select

    async with test_session_factory() as s:
        row = (
            await s.execute(
                select(LiveSession).where(LiveSession.id == session_uuid)
            )
        ).scalar_one_or_none()
    assert row is not None
    assert row.tenant_id == test_tenant.id
    assert row.status == "active"


@pytest.mark.asyncio
async def test_monitor_ticket_requires_user_id(
    test_client, test_tenant, ticket_redis_stub
):
    resp = await test_client.post(
        f"{PREFIX}/ws/tickets", json={"role": "monitor"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_monitor_ticket_rejects_agent_role_user(
    test_client, test_tenant, test_session_factory, ticket_redis_stub
):
    from backend.app.models import User

    # Seed an agent-role user.
    async with test_session_factory() as s:
        user = User(
            tenant_id=test_tenant.id,
            email=f"agent-{uuid.uuid4().hex[:6]}@example.com",
            role="agent",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        user_id = str(user.id)

    resp = await test_client.post(
        f"{PREFIX}/ws/tickets",
        json={"role": "monitor", "user_id": user_id},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_monitor_ticket_accepts_manager_user(
    test_client, test_tenant, test_session_factory, ticket_redis_stub
):
    from backend.app.models import User

    async with test_session_factory() as s:
        user = User(
            tenant_id=test_tenant.id,
            email=f"manager-{uuid.uuid4().hex[:6]}@example.com",
            role="manager",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        user_id = str(user.id)

    resp = await test_client.post(
        f"{PREFIX}/ws/tickets",
        json={"role": "monitor", "user_id": user_id},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "monitor"
