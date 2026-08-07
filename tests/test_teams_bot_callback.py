"""Tests for the versioned ``POST /teams/bot/callback`` contract.

Two layers, matching ``tests/test_teams_ingest.py``'s split:

* Pure parsing/validation (``services/teams_recording/bot_callback.py``)
  — no DB, no HTTP.
* HTTP-level, DB-backed tests via ``teams_test_client`` covering the
  secret-header gate, the three event types, and the "bridge into the
  live pipeline" behaviour for ``audio.available``.

The two PRE-EXISTING placeholder tests in
``test_teams_notification_validation.py``
(``test_bot_callback_returns_503_with_default_stub`` and
``test_bot_callback_returns_200_when_real_bot_registered``) are left
untouched and still pass: the endpoint keeps its "not deployed → 503"
gate first, and (with ``TEAMS_BOT_CALLBACK_SECRET`` unset, as in that
file) falls back to lenient best-effort acceptance for payloads that
don't match the v1 contract — see api/teams_recording.py's docstring.
"""

from __future__ import annotations

import pytest

from tests.test_teams_common import (  # noqa: F401 — fixture re-export
    AAD_TENANT_ID,
    seeded_teams_integration,
    teams_test_app,
    teams_test_client,
)

# parse_bot_callback tests below are sync (no I/O); HTTP-level tests are
# async and marked individually to avoid a module-wide asyncio-mark
# warning on the sync ones.


# ── parse_bot_callback (pure) ────────────────────────────────────────


def test_parse_session_started_event():
    from backend.app.services.teams_recording.bot_callback import (
        SESSION_STARTED,
        parse_bot_callback,
    )

    event = parse_bot_callback(
        {
            "version": "1",
            "event": "session.started",
            "call_id": "call-1",
            "session_id": "sess-1",
            "aad_tenant_id": "aad-1",
            "organizer": "alice@example.com",
            "join_url": "https://teams.microsoft.com/l/meetup-join/x",
        }
    )
    assert event.event == SESSION_STARTED
    assert event.call_id == "call-1"
    assert event.raw["organizer"] == "alice@example.com"


def test_parse_audio_available_requires_https_audio_url():
    from backend.app.services.teams_recording.bot_callback import (
        BotCallbackValidationError,
        parse_bot_callback,
    )

    base = {
        "version": "1",
        "event": "audio.available",
        "call_id": "call-1",
        "session_id": "sess-1",
        "aad_tenant_id": "aad-1",
    }
    with pytest.raises(BotCallbackValidationError, match="audio_url"):
        parse_bot_callback(dict(base))

    with pytest.raises(BotCallbackValidationError, match="HTTPS"):
        parse_bot_callback({**base, "audio_url": "http://insecure.example.com/a.wav"})

    event = parse_bot_callback(
        {**base, "audio_url": "https://blob.example.com/a.wav", "duration_seconds": 42.0}
    )
    assert event.audio_url == "https://blob.example.com/a.wav"
    assert event.duration_seconds == 42


def test_parse_rejects_unsupported_version():
    from backend.app.services.teams_recording.bot_callback import (
        BotCallbackValidationError,
        parse_bot_callback,
    )

    with pytest.raises(BotCallbackValidationError, match="version"):
        parse_bot_callback(
            {
                "version": "2",
                "event": "session.started",
                "call_id": "c",
                "session_id": "s",
                "aad_tenant_id": "a",
            }
        )


def test_parse_rejects_unknown_event():
    from backend.app.services.teams_recording.bot_callback import (
        BotCallbackValidationError,
        parse_bot_callback,
    )

    with pytest.raises(BotCallbackValidationError, match="event"):
        parse_bot_callback(
            {
                "version": "1",
                "event": "call.ringing",
                "call_id": "c",
                "session_id": "s",
                "aad_tenant_id": "a",
            }
        )


def test_parse_rejects_missing_correlation_ids():
    from backend.app.services.teams_recording.bot_callback import (
        BotCallbackValidationError,
        parse_bot_callback,
    )

    with pytest.raises(BotCallbackValidationError):
        parse_bot_callback({"version": "1", "event": "session.stopped", "call_id": "c"})


# ── HTTP-level: gating order + secret enforcement ───────────────────


@pytest.fixture(autouse=True)
def _reset_bot_registry():
    from backend.app.services.teams_recording.bot_interface import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


def _register_fake_deployed_bot():
    from backend.app.services.teams_recording.bot_interface import (
        MediaBotStatus,
        StubMediaBot,
        set_media_bot_factory,
    )

    class _FakeBot(StubMediaBot):
        name = "fake-real"

        def status(self) -> MediaBotStatus:
            return MediaBotStatus(deployed=True, reason="ok")

        def is_available(self) -> bool:
            return True

    set_media_bot_factory(_FakeBot)


@pytest.mark.asyncio
async def test_callback_503_before_secret_check_when_bot_not_deployed(teams_test_client):
    # No TEAMS_BOT_CALLBACK_SECRET set, no header sent, bot not deployed —
    # must still 503 (the deployed-check runs first).
    resp = await teams_test_client.post("/api/v1/teams/bot/callback", json={"event": "join"})
    assert resp.status_code == 503
    assert resp.json()["deployed"] is False


@pytest.mark.asyncio
async def test_callback_401_on_bad_secret_when_configured(teams_test_client, monkeypatch):
    from backend.app.config import get_settings

    _register_fake_deployed_bot()
    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_BOT_CALLBACK_SECRET", "the-real-secret")
    try:
        resp = await teams_test_client.post(
            "/api/v1/teams/bot/callback",
            json={"version": "1", "event": "session.started"},
            headers={"X-LINDA-Bot-Secret": "wrong"},
        )
        assert resp.status_code == 401
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_callback_400_on_malformed_payload_when_secret_configured(
    teams_test_client, monkeypatch
):
    from backend.app.config import get_settings

    _register_fake_deployed_bot()
    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_BOT_CALLBACK_SECRET", "the-real-secret")
    try:
        resp = await teams_test_client.post(
            "/api/v1/teams/bot/callback",
            json={"event": "not-a-real-event"},
            headers={"X-LINDA-Bot-Secret": "the-real-secret"},
        )
        assert resp.status_code == 400
    finally:
        get_settings.cache_clear()


# ── HTTP-level: persistence per event, secret configured + valid ────


@pytest.mark.asyncio
async def test_session_started_upserts_teams_call_record(
    teams_test_client, test_tenant, test_session_factory, seeded_teams_integration, monkeypatch
):
    from backend.app.config import get_settings
    from backend.app.models import TeamsCallRecord
    from sqlalchemy import select

    _register_fake_deployed_bot()
    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_BOT_CALLBACK_SECRET", "the-real-secret")
    try:
        resp = await teams_test_client.post(
            "/api/v1/teams/bot/callback",
            json={
                "version": "1",
                "event": "session.started",
                "call_id": "bot-call-1",
                "session_id": "bot-sess-1",
                "aad_tenant_id": AAD_TENANT_ID,
                "organizer": "bob@example.com",
                "join_url": "https://teams.microsoft.com/l/meetup-join/y",
            },
            headers={"X-LINDA-Bot-Secret": "the-real-secret"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "received": True,
            "event": "session.started",
            "action": "call_record_upserted",
            "call_id": "bot-call-1",
        }
    finally:
        get_settings.cache_clear()

    async with test_session_factory() as session:
        record = (
            await session.execute(
                select(TeamsCallRecord).where(TeamsCallRecord.tenant_id == test_tenant.id)
            )
        ).scalar_one()
        assert record.call_id == "bot-call-1"
        assert record.organizer == "bob@example.com"
        assert record.certification_status == "scaffold"  # bot IS "available" in this test


@pytest.mark.asyncio
async def test_audio_available_bridges_into_live_pipeline(
    teams_test_client, test_tenant, test_session_factory, seeded_teams_integration, monkeypatch
):
    from types import SimpleNamespace

    from backend.app.config import get_settings
    from backend.app.models import Interaction
    from sqlalchemy import select

    _register_fake_deployed_bot()
    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_BOT_CALLBACK_SECRET", "the-real-secret")

    # Celery isn't running in tests — swap in a fake ``.delay`` the same
    # way tests/test_upload_pipeline.py does for the other Celery
    # dispatch call sites in this codebase.
    dispatched = []
    import backend.app.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "process_voice_interaction",
        SimpleNamespace(delay=lambda interaction_id: dispatched.append(interaction_id)),
    )

    try:
        resp = await teams_test_client.post(
            "/api/v1/teams/bot/callback",
            json={
                "version": "1",
                "event": "audio.available",
                "call_id": "bot-call-2",
                "session_id": "bot-sess-2",
                "aad_tenant_id": AAD_TENANT_ID,
                "audio_url": "https://blob.example.com/recordings/bot-call-2.wav",
                "duration_seconds": 321,
                "caller_upn": "carol@example.com",
                "direction": "internal",
            },
            headers={"X-LINDA-Bot-Secret": "the-real-secret"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["received"] is True
        assert body["event"] == "audio.available"
        assert body["action"] == "interaction_created"
    finally:
        get_settings.cache_clear()

    async with test_session_factory() as session:
        interaction = (
            await session.execute(
                select(Interaction).where(Interaction.tenant_id == test_tenant.id)
            )
        ).scalar_one()
        assert interaction.audio_url == "https://blob.example.com/recordings/bot-call-2.wav"
        assert interaction.source == "teams_compliance"
        assert interaction.duration_seconds == 321
        assert interaction.caller_phone == "carol@example.com"
        assert interaction.thread_id == "bot-call-2"
        assert dispatched == [str(interaction.id)]


@pytest.mark.asyncio
async def test_bot_callback_unknown_tenant_is_skipped_gracefully(
    teams_test_client, monkeypatch
):
    from backend.app.config import get_settings

    _register_fake_deployed_bot()
    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_BOT_CALLBACK_SECRET", "the-real-secret")
    try:
        resp = await teams_test_client.post(
            "/api/v1/teams/bot/callback",
            json={
                "version": "1",
                "event": "session.stopped",
                "call_id": "orphan-call",
                "session_id": "orphan-sess",
                "aad_tenant_id": "some-other-aad-tenant",
            },
            headers={"X-LINDA-Bot-Secret": "the-real-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "skipped_unknown_tenant"
    finally:
        get_settings.cache_clear()
