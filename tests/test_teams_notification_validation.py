"""HTTP-level tests for ``backend.app.api.teams_recording``.

We don't boot the full ``main.app`` (it requires Postgres for lifespan).
Instead we mount the teams-recording router on a fresh FastAPI app —
the router has no DB dependencies in the scaffold round, so this is
sufficient and isolates the test from unrelated wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.teams_recording import router as teams_router
from backend.app.services.teams_recording.bot_interface import (
    MediaBotStatus,
    StubMediaBot,
    reset_for_tests,
    set_media_bot_factory,
)
from tests.test_teams_common import (  # noqa: F401 — fixture re-export
    AAD_TENANT_ID,
    seeded_teams_integration,
    teams_test_app,
    teams_test_client,
)


FIXTURES = Path(__file__).parent / "fixtures" / "teams"


@pytest.fixture
def client() -> TestClient:
    """Fresh FastAPI app + mounted router. No DB, no lifespan."""

    app = FastAPI()
    app.include_router(teams_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_bot_registry():
    """Every test gets the default stub bot back."""

    reset_for_tests()
    yield
    reset_for_tests()


# ── Validation handshake ─────────────────────────────────────────────


def test_validation_handshake_returns_token_as_plain_text(client: TestClient):
    response = client.post(
        "/teams/notification",
        params={"validationToken": "abc-validation-xyz"},
    )
    assert response.status_code == 200
    # Microsoft's contract: the body must be exactly the token, in
    # text/plain. ``application/json`` would be rejected by Graph.
    assert response.text == "abc-validation-xyz"
    assert response.headers["content-type"].startswith("text/plain")


def test_validation_handshake_with_empty_token_returns_400(client: TestClient):
    response = client.post(
        "/teams/notification",
        params={"validationToken": ""},
    )
    # Empty token is detected by is_validation_handshake (the key is
    # present) but rejected by validation_response_body — surfacing as
    # 400 keeps Graph from registering against a half-broken endpoint.
    assert response.status_code == 400


def test_validation_handshake_takes_precedence_over_body(client: TestClient):
    # Even when the request also has a body, if validationToken is
    # present we must echo it and ignore the body. Microsoft uses an
    # empty body for the handshake but defensiveness is cheap.
    response = client.post(
        "/teams/notification",
        params={"validationToken": "tok"},
        json={"value": []},
    )
    assert response.status_code == 200
    assert response.text == "tok"


# ── Notification batch parsing ──────────────────────────────────────
#
# These two now exercise the real (SQLite-backed) persistence path via
# ``teams_test_client`` — the plain synchronous ``client`` fixture above
# has no DB wired up, and the notification handler now genuinely reads/
# writes the DB (see ``services/teams_recording/ingest.py``). Full
# happy-path persistence assertions (seeded Integration → TeamsCallRecord
# / UcRecordingJob rows) live in ``tests/test_teams_ingest.py``; these
# two stay focused on "the batch parses and 202s" plus the unknown-
# tenant skip path, since no Integration is seeded here.


@pytest.mark.asyncio
async def test_notification_batch_call_record_returns_202(teams_test_client):
    payload = json.loads((FIXTURES / "notification_call_record.json").read_text())
    response = await teams_test_client.post("/api/v1/teams/notification", json=payload)
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 1
    # No teams_compliance Integration is seeded for AAD_TENANT_ID in this
    # test, so the entry resolves to the graceful skip path rather than
    # persisting anything.
    assert body["results"] == [{"action": "skipped_unknown_tenant"}]


@pytest.mark.asyncio
async def test_notification_batch_recording_returns_202(teams_test_client):
    payload = json.loads(
        (FIXTURES / "notification_recording_created.json").read_text()
    )
    response = await teams_test_client.post("/api/v1/teams/notification", json=payload)
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 2
    assert body["results"] == [
        {"action": "skipped_unknown_tenant"},
        {"action": "skipped_unknown_tenant"},
    ]


def test_notification_with_invalid_json_returns_400(client: TestClient):
    response = client.post(
        "/teams/notification",
        data=b"this is not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert "JSON" in response.json()["error"]


def test_notification_with_missing_value_array_returns_400(client: TestClient):
    response = client.post("/teams/notification", json={"foo": "bar"})
    assert response.status_code == 400
    assert "value" in response.json()["error"]


def test_notification_with_malformed_entry_returns_400(client: TestClient):
    response = client.post(
        "/teams/notification",
        json={"value": [{"subscriptionId": "x"}]},
    )
    assert response.status_code == 400


# ── Bot callback placeholder ────────────────────────────────────────


def test_bot_callback_returns_503_with_default_stub(client: TestClient):
    response = client.post("/teams/bot/callback", json={"event": "join"})
    assert response.status_code == 503
    body = response.json()
    assert body["deployed"] is False
    assert "not deployed" in body["reason"].lower()


def test_bot_callback_returns_200_when_real_bot_registered(client: TestClient):
    """When the .NET bridge eventually registers a deployed bot, the
    callback flips to 200. We simulate that here to pin the contract."""

    class _RealBotStub(StubMediaBot):
        name = "fake-real"

        def status(self) -> MediaBotStatus:
            return MediaBotStatus(deployed=True, reason="ok")

        def is_available(self) -> bool:
            return True

    set_media_bot_factory(_RealBotStub)
    response = client.post("/teams/bot/callback", json={"event": "join"})
    assert response.status_code == 200
    assert response.json() == {"received": True}


# ── Router registration smoke ───────────────────────────────────────


def test_router_paths_are_registered():
    """Catches a future regression where someone deletes the
    ``include_router`` lines from main.py without realising."""

    paths = {route.path for route in teams_router.routes}  # type: ignore[attr-defined]
    assert "/teams/notification" in paths
    assert "/teams/bot/callback" in paths
