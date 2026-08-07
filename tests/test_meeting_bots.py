"""Tests for the meeting-bot vendor connector.

Covers:

* Pure helpers — platform detection, vendor-status normalization, PCM
  frame extraction (JSON envelope + binary fallback).
* ``services.meeting_bots`` — vendor HTTP client (mocked via ``respx``)
  + Redis correlation mapping (mocked via an in-memory fake, same
  pattern as ``test_telnyx_session_map.py`` / ``test_siprec_dispatch.py``).
* ``api.meeting_bots`` REST routes — dispatch/list/get/stop, gated by
  ``require_feature("meeting_assist")`` + ``require_scope("live:write"
  /"live:read")``, following the ``test_coaching_sessions.py`` pattern
  of overriding ``get_current_tenant`` + ``get_current_principal`` on a
  focused FastAPI app.
* The vendor webhook — shared-secret auth, Redis-mapping resolution,
  terminal-status finalization (with ``websocket._dispatch_batch_analysis``
  monkeypatched so no Celery/DB pipeline runs).
* The audio ingress WebSocket — auth-reject path and a happy path with
  a fake Deepgram connection (Deepgram SDK isn't exercised for real).
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

# ── Fakes ────────────────────────────────────────────────────────────


class FakePipe:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._ops: List[tuple] = []

    def rpush(self, key: str, val: str):
        self._ops.append(("rpush", key, val))
        return self

    def expire(self, key: str, ttl: int):
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        for op, key, val in self._ops:
            if op == "rpush":
                self._redis.lists.setdefault(key, []).append(val)
            elif op == "expire":
                self._redis.expires[key] = val
        return None


class FakeRedis:
    """Shared in-memory async Redis fake — covers set/get/delete,
    publish, and the rpush/expire pipeline the ingress WS uses."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.lists: Dict[str, List[str]] = {}
        self.expires: Dict[str, int] = {}
        self.published: List[tuple] = []
        self.closed_count = 0

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return 1

    async def publish(self, channel, message):
        self.published.append((channel, message))
        return 1

    def pipeline(self, transaction: bool = False):
        return FakePipe(self)

    async def aclose(self):
        self.closed_count += 1


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch ``redis.asyncio.from_url`` everywhere so every module that
    does a local ``import redis.asyncio as aioredis`` (services + api)
    talks to the same in-memory fake."""
    fake = FakeRedis()
    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: fake)
    return fake


@pytest.fixture
def recall_settings(monkeypatch):
    """Configure Recall + webhook + Redis settings and drop the
    ``get_settings`` lru_cache so every module sees them."""
    from backend.app.config import get_settings

    monkeypatch.setenv("RECALL_AI_API_KEY", "fixture-recall-key")
    monkeypatch.setenv("RECALL_API_BASE", "https://recall.fixture.test")
    monkeypatch.setenv("MEETING_BOT_WEBHOOK_SECRET", "fixture-webhook-secret")
    monkeypatch.setenv("REDIS_URL", "redis://fake/0")
    monkeypatch.setenv("PUBLIC_WEBHOOK_BASE_URL", "")
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()


# ── Pure helpers ─────────────────────────────────────────────────────


def test_detect_platform_zoom():
    from backend.app.services.meeting_bots import detect_platform

    assert detect_platform("https://zoom.us/j/1234567890") == "zoom"


def test_detect_platform_meet():
    from backend.app.services.meeting_bots import detect_platform

    assert detect_platform("https://meet.google.com/abc-defg-hij") == "meet"


def test_detect_platform_teams():
    from backend.app.services.meeting_bots import detect_platform

    assert detect_platform("https://teams.microsoft.com/l/meetup-join/x") == "teams"
    assert detect_platform("https://teams.live.com/meet/123") == "teams"


def test_detect_platform_unknown():
    from backend.app.services.meeting_bots import detect_platform

    assert detect_platform("https://example.com/some-meeting") == "unknown"
    assert detect_platform("") == "unknown"


def test_normalize_vendor_status_mapping():
    from backend.app.api.meeting_bots import _normalize_vendor_status

    assert _normalize_vendor_status("joining_call") == "joining"
    assert _normalize_vendor_status("in_call_recording") == "in_call"
    assert _normalize_vendor_status("in_call_not_recording") == "in_call"
    assert _normalize_vendor_status("call_ended") == "done"
    assert _normalize_vendor_status("done") == "done"
    assert _normalize_vendor_status("fatal_error") == "failed"
    assert _normalize_vendor_status("") == "in_call"


def test_extract_pcm_binary_frame():
    from backend.app.api.meeting_bots import _extract_pcm

    raw = b"\x01\x02\x03\x04"
    assert _extract_pcm({"bytes": raw}) == raw


def test_extract_pcm_json_envelope():
    from backend.app.api.meeting_bots import _extract_pcm

    pcm = b"\x00\x01" * 8
    envelope = json.dumps(
        {
            "event": "audio_mixed_raw.data",
            "data": {
                "data": {"buffer": base64.b64encode(pcm).decode(), "timestamp": 1.5},
                "bot": {"id": "vendor-bot-1"},
            },
        }
    )
    assert _extract_pcm({"text": envelope}) == pcm


def test_extract_pcm_wrong_event_ignored():
    from backend.app.api.meeting_bots import _extract_pcm

    envelope = json.dumps({"event": "something_else", "data": {}})
    assert _extract_pcm({"text": envelope}) is None


def test_extract_pcm_malformed_json_ignored():
    from backend.app.api.meeting_bots import _extract_pcm

    assert _extract_pcm({"text": "not json"}) is None


def test_extract_pcm_missing_buffer_ignored():
    from backend.app.api.meeting_bots import _extract_pcm

    envelope = json.dumps({"event": "audio_mixed_raw.data", "data": {"data": {}}})
    assert _extract_pcm({"text": envelope}) is None


def test_extract_pcm_empty_frame():
    from backend.app.api.meeting_bots import _extract_pcm

    assert _extract_pcm({}) is None


# ── Redis mapping round trip ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_remember_resolve_forget_round_trip(fake_redis):
    from backend.app.services.meeting_bots import (
        _remember_meetingbot,
        forget_meetingbot,
        resolve_meetingbot,
    )

    await _remember_meetingbot(
        "job-123",
        tenant_id="t-1",
        session_id="s-1",
        job_id="job-123",
        token="tok-abc",
    )
    ctx = await resolve_meetingbot("job-123")
    assert ctx == {
        "tenant_id": "t-1",
        "session_id": "s-1",
        "job_id": "job-123",
        "token": "tok-abc",
    }
    assert fake_redis.ttls["meetingbot:job-123"] > 0

    await forget_meetingbot("job-123")
    assert await resolve_meetingbot("job-123") is None


@pytest.mark.asyncio
async def test_resolve_meetingbot_missing_returns_none(fake_redis):
    from backend.app.services.meeting_bots import resolve_meetingbot

    assert await resolve_meetingbot("never-set") is None


# ── services.meeting_bots.create_bot / stop_bot ─────────────────────


@pytest_asyncio.fixture
async def enterprise_tenant(test_tenant):
    test_tenant.plan_tier = "enterprise"
    return test_tenant


@respx.mock
@pytest.mark.asyncio
async def test_create_bot_success(
    fake_redis, recall_settings, test_session_factory, enterprise_tenant
):
    from backend.app.services.meeting_bots import create_bot
    from backend.app.models import LiveSession, MeetingBotJob

    respx.post("https://recall.fixture.test/api/v1/bot").mock(
        return_value=Response(
            201,
            json={"id": "vendor-bot-1", "status": {"code": "joining_call"}},
        )
    )

    async with test_session_factory() as db:
        result = await create_bot(
            db,
            tenant=enterprise_tenant,
            meeting_url="https://zoom.us/j/1234567890",
            requested_by_user_id=None,
            request_base_url="http://test/",
        )

        assert result.job.status == "joining"
        assert result.job.bot_id == "vendor-bot-1"
        assert result.job.platform == "zoom"
        assert result.session.source == "meeting_bot"
        assert result.session.external_call_id == "vendor-bot-1"

        job_row = await db.get(MeetingBotJob, result.job.id)
        assert job_row is not None
        assert job_row.bot_id == "vendor-bot-1"
        session_row = await db.get(LiveSession, result.session.id)
        assert session_row is not None
        assert session_row.external_call_id == "vendor-bot-1"

    # Both the pre-vendor-response (our own job id) and post-response
    # (vendor bot id) Redis correlation keys must resolve to the same
    # context, with the SAME ingress token.
    from backend.app.services.meeting_bots import resolve_meetingbot

    by_job_id = await resolve_meetingbot(str(result.job.id))
    by_vendor_id = await resolve_meetingbot("vendor-bot-1")
    assert by_job_id is not None and by_vendor_id is not None
    assert by_job_id["token"] == by_vendor_id["token"] == result.ingress_token
    assert by_job_id["session_id"] == by_vendor_id["session_id"] == str(result.session.id)

    # The request sent to the vendor carries our metadata + the
    # WS destination URL (using our own job id, not the not-yet-known
    # vendor id — see services.meeting_bots module docstring).
    sent_request = respx.calls[0].request
    sent_payload = json.loads(sent_request.content)
    assert sent_payload["meeting_url"] == "https://zoom.us/j/1234567890"
    assert sent_payload["metadata"]["job_id"] == str(result.job.id)
    ws_url = sent_payload["recording_config"]["realtime_endpoints"][0]["url"]
    assert f"/ws/meeting-bots/{result.job.id}" in ws_url
    assert f"token={result.ingress_token}" in ws_url


@respx.mock
@pytest.mark.asyncio
async def test_create_bot_vendor_failure_marks_job_failed(
    fake_redis, recall_settings, test_session_factory, enterprise_tenant
):
    from backend.app.services.meeting_bots import MeetingBotError, create_bot
    from backend.app.models import MeetingBotJob

    respx.post("https://recall.fixture.test/api/v1/bot").mock(
        return_value=Response(500, text="vendor is down")
    )

    async with test_session_factory() as db:
        with pytest.raises(MeetingBotError):
            await create_bot(
                db,
                tenant=enterprise_tenant,
                meeting_url="https://meet.google.com/abc-defg",
                requested_by_user_id=None,
                request_base_url="http://test/",
            )

        rows = (await db.execute(__import__("sqlalchemy").select(MeetingBotJob))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert "500" in (rows[0].last_error or "")


@pytest.mark.asyncio
async def test_create_bot_missing_api_key_raises(
    fake_redis, recall_settings, test_session_factory, enterprise_tenant, monkeypatch
):
    from backend.app.config import get_settings
    from backend.app.services.meeting_bots import MeetingBotError, create_bot

    monkeypatch.delenv("RECALL_AI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        async with test_session_factory() as db:
            with pytest.raises(MeetingBotError, match="RECALL_AI_API_KEY"):
                await create_bot(
                    db,
                    tenant=enterprise_tenant,
                    meeting_url="https://zoom.us/j/1",
                    requested_by_user_id=None,
                    request_base_url="http://test/",
                )
    finally:
        get_settings.cache_clear()


@respx.mock
@pytest.mark.asyncio
async def test_stop_bot_success(fake_redis, recall_settings):
    from backend.app.models import MeetingBotJob
    from backend.app.services.meeting_bots import stop_bot

    respx.post("https://recall.fixture.test/api/v1/bot/vendor-bot-1/leave_call").mock(
        return_value=Response(200, json={"ok": True})
    )
    job = MeetingBotJob(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        bot_id="vendor-bot-1",
        meeting_url="https://zoom.us/j/1",
        status="in_call",
    )
    await stop_bot(job)  # must not raise


@respx.mock
@pytest.mark.asyncio
async def test_stop_bot_vendor_error_raises(fake_redis, recall_settings):
    from backend.app.models import MeetingBotJob
    from backend.app.services.meeting_bots import MeetingBotError, stop_bot

    respx.post("https://recall.fixture.test/api/v1/bot/vendor-bot-1/leave_call").mock(
        return_value=Response(503, text="nope")
    )
    job = MeetingBotJob(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        bot_id="vendor-bot-1",
        meeting_url="https://zoom.us/j/1",
        status="in_call",
    )
    with pytest.raises(MeetingBotError):
        await stop_bot(job)


@pytest.mark.asyncio
async def test_stop_bot_without_bot_id_is_noop(fake_redis, recall_settings):
    from backend.app.models import MeetingBotJob
    from backend.app.services.meeting_bots import stop_bot

    job = MeetingBotJob(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        bot_id=None,
        meeting_url="https://zoom.us/j/1",
        status="requested",
    )
    await stop_bot(job)  # never calls the vendor; must not raise


# ── Router: REST endpoints ───────────────────────────────────────────


def _principal_for(tenant, *, role="admin", source="session", scopes=None):
    from backend.app.auth import AuthPrincipal

    return AuthPrincipal(
        tenant=tenant,
        user=None,
        role=role,
        source=source,
        scopes=scopes if scopes is not None else ["*"],
    )


@pytest_asyncio.fixture
async def mb_app(test_session_factory, test_tenant, recall_settings):
    from fastapi import FastAPI

    from backend.app.api.meeting_bots import router as meeting_bots_router
    from backend.app.auth import get_current_principal, get_current_tenant
    from backend.app.db import get_db

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    state: Dict[str, Any] = {
        "tenant": test_tenant,
        "role": "admin",
        "source": "session",
        "scopes": ["*"],
    }

    async def _override_get_tenant():
        return state["tenant"]

    async def _override_get_principal():
        return _principal_for(
            state["tenant"],
            role=state["role"],
            source=state["source"],
            scopes=state["scopes"],
        )

    app = FastAPI()
    app.include_router(meeting_bots_router, prefix="/api/v1", tags=["meeting-bots"])
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_tenant] = _override_get_tenant
    app.dependency_overrides[get_current_principal] = _override_get_principal
    app.state.test_state = state  # type: ignore[attr-defined]
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def mb_client(mb_app):
    transport = ASGITransport(app=mb_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_dispatch_requires_meeting_assist_feature(mb_client, fake_redis):
    # test_tenant defaults to plan_tier="sandbox" (meeting_assist=False).
    resp = await mb_client.post(
        "/api/v1/meeting-bots", json={"meeting_url": "https://zoom.us/j/1"}
    )
    assert resp.status_code == 402


@pytest.mark.asyncio
async def test_dispatch_api_key_without_scope_is_forbidden(mb_app, mb_client, fake_redis):
    mb_app.state.test_state["tenant"].plan_tier = "enterprise"
    mb_app.state.test_state["source"] = "api_key"
    mb_app.state.test_state["scopes"] = []

    resp = await mb_client.post(
        "/api/v1/meeting-bots", json={"meeting_url": "https://zoom.us/j/1"}
    )
    assert resp.status_code == 403


@respx.mock
@pytest.mark.asyncio
async def test_dispatch_success_returns_job(mb_app, mb_client, fake_redis):
    mb_app.state.test_state["tenant"].plan_tier = "enterprise"

    respx.post("https://recall.fixture.test/api/v1/bot").mock(
        return_value=Response(201, json={"id": "vendor-bot-42"})
    )

    resp = await mb_client.post(
        "/api/v1/meeting-bots", json={"meeting_url": "https://meet.google.com/abc-defg"}
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["status"] == "joining"
    assert payload["platform"] == "meet"
    assert payload["bot_id"] == "vendor-bot-42"
    assert payload["session_id"] is not None
    assert payload["monitor_ws_path"] == f"/ws/monitor/{payload['session_id']}"
    assert payload["embed_path"] == f"/embed/live/{payload['session_id']}"
    return payload


@pytest.mark.asyncio
async def test_dispatch_rejects_short_url(mb_app, mb_client, fake_redis):
    mb_app.state.test_state["tenant"].plan_tier = "enterprise"
    resp = await mb_client.post("/api/v1/meeting-bots", json={"meeting_url": "x"})
    assert resp.status_code == 422


@respx.mock
@pytest.mark.asyncio
async def test_list_and_get_meeting_bots(mb_app, mb_client, fake_redis):
    mb_app.state.test_state["tenant"].plan_tier = "enterprise"
    respx.post("https://recall.fixture.test/api/v1/bot").mock(
        return_value=Response(201, json={"id": "vendor-bot-list-1"})
    )
    created = await mb_client.post(
        "/api/v1/meeting-bots", json={"meeting_url": "https://zoom.us/j/999"}
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    listed = await mb_client.get("/api/v1/meeting-bots")
    assert listed.status_code == 200
    ids = [row["id"] for row in listed.json()]
    assert job_id in ids

    got = await mb_client.get(f"/api/v1/meeting-bots/{job_id}")
    assert got.status_code == 200
    assert got.json()["id"] == job_id


@pytest.mark.asyncio
async def test_get_unknown_job_404s(mb_app, mb_client, fake_redis):
    mb_app.state.test_state["tenant"].plan_tier = "enterprise"
    resp = await mb_client.get(f"/api/v1/meeting-bots/{uuid.uuid4()}")
    assert resp.status_code == 404


@respx.mock
@pytest.mark.asyncio
async def test_stop_meeting_bot_finalizes(mb_app, mb_client, fake_redis, monkeypatch):
    mb_app.state.test_state["tenant"].plan_tier = "enterprise"
    respx.post("https://recall.fixture.test/api/v1/bot").mock(
        return_value=Response(201, json={"id": "vendor-bot-stop-1"})
    )
    respx.post(
        "https://recall.fixture.test/api/v1/bot/vendor-bot-stop-1/leave_call"
    ).mock(return_value=Response(200, json={"ok": True}))

    created = await mb_client.post(
        "/api/v1/meeting-bots", json={"meeting_url": "https://zoom.us/j/stop"}
    )
    job_id = created.json()["id"]

    finalized: List[str] = []

    async def fake_dispatch(_redis, session_id):
        finalized.append(session_id)

    import backend.app.api.websocket as websocket_module

    monkeypatch.setattr(websocket_module, "_dispatch_batch_analysis", fake_dispatch)

    resp = await mb_client.delete(f"/api/v1/meeting-bots/{job_id}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "done"
    assert payload["ended_at"] is not None
    assert finalized == [payload["session_id"]]

    from backend.app.services.meeting_bots import resolve_meetingbot

    assert await resolve_meetingbot("vendor-bot-stop-1") is None
    assert await resolve_meetingbot(job_id) is None


@pytest.mark.asyncio
async def test_stop_already_terminal_job_is_noop(mb_app, mb_client, fake_redis, test_session_factory):
    from backend.app.models import MeetingBotJob

    tenant = mb_app.state.test_state["tenant"]
    tenant.plan_tier = "enterprise"

    async with test_session_factory() as db:
        job = MeetingBotJob(
            tenant_id=tenant.id,
            meeting_url="https://zoom.us/j/done",
            status="done",
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    resp = await mb_client.delete(f"/api/v1/meeting-bots/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


# ── Webhook ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def webhook_app(test_session_factory, recall_settings):
    from fastapi import FastAPI

    from backend.app.api.meeting_bots import router as meeting_bots_router
    from backend.app.db import get_db

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(meeting_bots_router, prefix="/api/v1", tags=["meeting-bots"])
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def webhook_client(webhook_app):
    transport = ASGITransport(app=webhook_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed_job(test_session_factory, tenant_id, *, bot_id, status="in_call"):
    from backend.app.models import LiveSession, MeetingBotJob

    async with test_session_factory() as db:
        session = LiveSession(
            tenant_id=tenant_id, agent_id=tenant_id, source="meeting_bot", status="active"
        )
        db.add(session)
        await db.flush()
        job = MeetingBotJob(
            tenant_id=tenant_id,
            live_session_id=session.id,
            bot_id=bot_id,
            meeting_url="https://zoom.us/j/webhook",
            platform="zoom",
            status=status,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job, session


@pytest.mark.asyncio
async def test_webhook_missing_secret_is_401(webhook_client, fake_redis):
    resp = await webhook_client.post(
        "/api/v1/meeting-bots/webhook",
        json={"bot": {"id": "x"}, "status": {"code": "in_call"}},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_wrong_secret_is_401(webhook_client, fake_redis):
    resp = await webhook_client.post(
        "/api/v1/meeting-bots/webhook",
        json={"bot": {"id": "x"}, "status": {"code": "in_call"}},
        headers={"X-Meeting-Bot-Secret": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_unknown_bot_is_404(webhook_client, fake_redis):
    resp = await webhook_client.post(
        "/api/v1/meeting-bots/webhook",
        json={"bot": {"id": "never-registered"}, "status": {"code": "in_call"}},
        headers={"X-Meeting-Bot-Secret": "fixture-webhook-secret"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_webhook_in_call_updates_status_without_finalizing(
    webhook_client, fake_redis, test_session_factory, test_tenant, monkeypatch
):
    from backend.app.services.meeting_bots import _remember_meetingbot
    from backend.app.models import MeetingBotJob

    job, session = await _seed_job(
        test_session_factory, test_tenant.id, bot_id="vendor-in-call", status="joining"
    )
    await _remember_meetingbot(
        "vendor-in-call",
        tenant_id=str(test_tenant.id),
        session_id=str(session.id),
        job_id=str(job.id),
        token="tok",
    )

    finalized: List[str] = []

    async def fake_dispatch(_redis, session_id):
        finalized.append(session_id)

    import backend.app.api.websocket as websocket_module

    monkeypatch.setattr(websocket_module, "_dispatch_batch_analysis", fake_dispatch)

    resp = await webhook_client.post(
        "/api/v1/meeting-bots/webhook",
        json={"bot": {"id": "vendor-in-call"}, "status": {"code": "in_call_recording"}},
        headers={"X-Meeting-Bot-Secret": "fixture-webhook-secret"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_status"] == "in_call"
    assert finalized == []

    async with test_session_factory() as db:
        row = await db.get(MeetingBotJob, job.id)
        assert row.status == "in_call"
        assert row.ended_at is None


@pytest.mark.asyncio
async def test_webhook_terminal_status_finalizes_and_forgets_mapping(
    webhook_client, fake_redis, test_session_factory, test_tenant, monkeypatch
):
    from backend.app.services.meeting_bots import _remember_meetingbot, resolve_meetingbot
    from backend.app.models import MeetingBotJob

    job, session = await _seed_job(
        test_session_factory, test_tenant.id, bot_id="vendor-terminal", status="in_call"
    )
    await _remember_meetingbot(
        "vendor-terminal",
        tenant_id=str(test_tenant.id),
        session_id=str(session.id),
        job_id=str(job.id),
        token="tok",
    )

    finalized: List[str] = []

    async def fake_dispatch(_redis, session_id):
        finalized.append(session_id)

    import backend.app.api.websocket as websocket_module

    monkeypatch.setattr(websocket_module, "_dispatch_batch_analysis", fake_dispatch)

    resp = await webhook_client.post(
        "/api/v1/meeting-bots/webhook",
        json={"bot": {"id": "vendor-terminal"}, "status": {"code": "call_ended"}},
        headers={"X-Meeting-Bot-Secret": "fixture-webhook-secret"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_status"] == "done"
    assert finalized == [str(session.id)]

    async with test_session_factory() as db:
        row = await db.get(MeetingBotJob, job.id)
        assert row.status == "done"
        assert row.ended_at is not None

    assert await resolve_meetingbot("vendor-terminal") is None


@pytest.mark.asyncio
async def test_webhook_fatal_status_sets_last_error(
    webhook_client, fake_redis, test_session_factory, test_tenant, monkeypatch
):
    from backend.app.services.meeting_bots import _remember_meetingbot
    from backend.app.models import MeetingBotJob

    job, session = await _seed_job(
        test_session_factory, test_tenant.id, bot_id="vendor-fatal", status="in_call"
    )
    await _remember_meetingbot(
        "vendor-fatal",
        tenant_id=str(test_tenant.id),
        session_id=str(session.id),
        job_id=str(job.id),
        token="tok",
    )

    async def fake_dispatch(_redis, session_id):
        return None

    import backend.app.api.websocket as websocket_module

    monkeypatch.setattr(websocket_module, "_dispatch_batch_analysis", fake_dispatch)

    resp = await webhook_client.post(
        "/api/v1/meeting-bots/webhook",
        json={
            "bot": {"id": "vendor-fatal"},
            "status": {"code": "fatal_error", "message": "bot crashed"},
        },
        headers={"X-Meeting-Bot-Secret": "fixture-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["job_status"] == "failed"

    async with test_session_factory() as db:
        row = await db.get(MeetingBotJob, job.id)
        assert row.status == "failed"
        assert row.last_error == "bot crashed"


# ── WebSocket ingress ────────────────────────────────────────────────


class FakeDgConnection:
    """Fake Deepgram live connection. ``send`` synthesizes one final
    transcript event per call so tests stay deterministic without
    threading into the real SDK's callback machinery."""

    def __init__(self) -> None:
        self.handlers: Dict[str, Any] = {}
        self.started: Optional[dict] = None
        self.sent: List[bytes] = []
        self.finished = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def start(self, opts):
        self.started = opts

    async def send(self, data: bytes):
        self.sent.append(data)
        handler = self.handlers.get("Results")
        if handler is not None:
            result = SimpleNamespace(
                is_final=True,
                channel=SimpleNamespace(
                    alternatives=[
                        SimpleNamespace(
                            transcript="hello from the meeting",
                            words=[SimpleNamespace(speaker=1)],
                        )
                    ]
                ),
            )
            await handler(None, result)

    async def finish(self):
        self.finished = True


@pytest.fixture
def fake_deepgram(monkeypatch):
    created: List[FakeDgConnection] = []

    class _FakeDeepgramClient:
        def __init__(self, api_key):
            self.api_key = api_key
            conn = FakeDgConnection()
            created.append(conn)
            self.listen = SimpleNamespace(live=SimpleNamespace(v=lambda ver, c=conn: c))

    import deepgram

    monkeypatch.setattr(deepgram, "DeepgramClient", _FakeDeepgramClient)
    return created


@pytest_asyncio.fixture
async def ws_app(recall_settings):
    from fastapi import FastAPI

    from backend.app.api.meeting_bots import router as meeting_bots_router

    app = FastAPI()
    app.include_router(meeting_bots_router, prefix="/api/v1", tags=["meeting-bots"])
    return app


def test_ws_ingress_rejects_without_mapping(ws_app, fake_redis):
    from starlette.testclient import WebSocketDisconnect
    from starlette.testclient import TestClient

    client = TestClient(ws_app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws/meeting-bots/unknown-bot?token=nope"):
            pass


def test_ws_ingress_rejects_bad_token(ws_app, fake_redis):
    from starlette.testclient import WebSocketDisconnect, TestClient
    import asyncio

    async def _seed():
        from backend.app.services.meeting_bots import _remember_meetingbot

        await _remember_meetingbot(
            "bot-badtoken",
            tenant_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
            job_id=str(uuid.uuid4()),
            token="correct-token",
        )

    asyncio.get_event_loop().run_until_complete(_seed())

    client = TestClient(ws_app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/ws/meeting-bots/bot-badtoken?token=wrong-token"
        ):
            pass


def test_ws_ingress_streams_audio_and_publishes_transcript(
    ws_app, fake_redis, fake_deepgram
):
    import asyncio

    from starlette.testclient import TestClient

    tenant_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    async def _seed():
        from backend.app.services.meeting_bots import _remember_meetingbot

        await _remember_meetingbot(
            "bot-happy-path",
            tenant_id=tenant_id,
            session_id=session_id,
            job_id=str(uuid.uuid4()),
            token="tok-happy",
        )

    asyncio.get_event_loop().run_until_complete(_seed())

    client = TestClient(ws_app)
    pcm = b"\x11\x22" * 4
    envelope = json.dumps(
        {
            "event": "audio_mixed_raw.data",
            "data": {"data": {"buffer": base64.b64encode(pcm).decode()}},
        }
    )
    with client.websocket_connect(
        f"/api/v1/ws/meeting-bots/bot-happy-path?token=tok-happy"
    ) as ws:
        ws.send_bytes(b"\xaa\xbb\xcc\xdd")
        ws.send_text(envelope)

    assert len(fake_deepgram) == 1
    conn = fake_deepgram[0]
    assert conn.started["encoding"] == "linear16"
    assert conn.started["sample_rate"] == 16000
    assert b"\xaa\xbb\xcc\xdd" in conn.sent
    assert pcm in conn.sent
    assert conn.finished is True

    # Each send() synthesized one final transcript, published + buffered
    # under the resolved session id.
    events_channel = f"live:{session_id}:events"
    buffer_key = f"live:{session_id}:buffer"
    assert any(ch == events_channel for ch, _ in fake_redis.published)
    assert buffer_key in fake_redis.lists
    seg = json.loads(fake_redis.lists[buffer_key][0])
    assert seg["text"] == "hello from the meeting"
    assert seg["speaker"] == 1
