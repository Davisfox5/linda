"""Notification → persistence tests for Teams compliance recording.

Mirrors ``tests/test_uc_ringcentral.py``'s shape (webhook →
UcRecordingJob → fetch happy path, plus a duplicate-delivery
idempotency test) but for the Graph change-notification batch
endpoint, and adds coverage for the ``TeamsCallRecord`` side (the
``communications/callRecords`` resource has no audio to fetch).
"""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from tests.test_teams_common import (  # noqa: F401 — fixture re-export
    AAD_TENANT_ID,
    load_fixture,
    seeded_teams_integration,
    teams_test_app,
    teams_test_client,
)

pytestmark = pytest.mark.asyncio


def _sync(fn):
    """Undo the module-level asyncio mark for the one plain-sync test
    below (``pytest.mark.asyncio`` on a non-async function is a warning)."""
    return pytest.mark.asyncio(None)(fn) if False else fn


# ── resolve_teams_integration / notification_to_uc_event (pure logic) ──


async def test_resolve_teams_integration_matches_by_aad_tenant_id(
    test_session_factory, test_tenant, seeded_teams_integration
):
    from backend.app.services.teams_recording.ingest import resolve_teams_integration

    async with test_session_factory() as session:
        found = await resolve_teams_integration(session, aad_tenant_id=AAD_TENANT_ID)
        assert found is not None
        assert found.tenant_id == test_tenant.id

        missing = await resolve_teams_integration(session, aad_tenant_id="unknown-tenant")
        assert missing is None

        empty = await resolve_teams_integration(session, aad_tenant_id=None)
        assert empty is None


def test_notification_to_uc_event_extracts_meeting_and_recording_ids():
    from backend.app.services.teams_recording.ingest import notification_to_uc_event
    from backend.app.services.teams_recording.subscriptions import parse_notifications

    payload = load_fixture("notification_recording_created.json")
    notes = parse_notifications(payload, expected_client_state="scaffold-shared-secret")
    event = notification_to_uc_event(notes[0])
    assert event.provider == "teams_compliance"
    assert event.external_call_id == "MSpkLTk5"
    assert event.recording_id == "RG9jLTk5"
    assert event.recording_url is None
    assert event.raw["odata_id"] == (
        "communications/onlineMeetings/MSpkLTk5/recordings/RG9jLTk5"
    )


# ── HTTP-level: callRecords resource → TeamsCallRecord ──────────────────


async def test_call_record_notification_upserts_teams_call_record(
    teams_test_client, test_tenant, test_session_factory, seeded_teams_integration
):
    from backend.app.models import TeamsCallRecord

    payload = load_fixture("notification_call_record.json")
    resp = await teams_test_client.post("/api/v1/teams/notification", json=payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] == 1
    assert body["results"] == [
        {"action": "call_record_upserted", "call_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    ]

    async with test_session_factory() as session:
        record = (
            await session.execute(
                select(TeamsCallRecord).where(TeamsCallRecord.tenant_id == test_tenant.id)
            )
        ).scalar_one()
        assert record.call_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        # No media bot is deployed (StubMediaBot) — an observed call we
        # couldn't have recorded is the documented "bot_required" state.
        assert record.certification_status == "bot_required"


async def test_call_record_notification_is_idempotent_on_replay(
    teams_test_client, test_tenant, test_session_factory, seeded_teams_integration
):
    from backend.app.models import TeamsCallRecord

    payload = load_fixture("notification_call_record.json")
    await teams_test_client.post("/api/v1/teams/notification", json=payload)
    await teams_test_client.post("/api/v1/teams/notification", json=payload)

    async with test_session_factory() as session:
        records = (
            await session.execute(
                select(TeamsCallRecord).where(TeamsCallRecord.tenant_id == test_tenant.id)
            )
        ).scalars().all()
        assert len(records) == 1


# ── HTTP-level: onlineMeetings/getAllRecordings → UcRecordingJob ────────


async def test_recording_notification_upserts_uc_recording_job_and_enqueues(
    teams_test_client,
    test_tenant,
    test_session_factory,
    seeded_teams_integration,
    monkeypatch,
):
    from backend.app.models import UcRecordingJob
    from backend.app.services.teams_recording import ingest as ingest_module

    enqueued = []
    monkeypatch.setattr(
        ingest_module, "_enqueue_fetch", lambda jid: enqueued.append(str(jid))
    )

    payload = load_fixture("notification_recording_created.json")
    resp = await teams_test_client.post("/api/v1/teams/notification", json=payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] == 2
    # Both entries in the fixture point at the same meeting/recording —
    # one job total, but each notification re-triggers the dispatch
    # while the job is still "pending" (identical to how
    # api/uc_telephony.py's own upsert-then-dispatch behaves on a
    # same-batch duplicate).
    assert [r["action"] for r in body["results"]] == [
        "recording_job_upserted",
        "recording_job_upserted",
    ]
    assert len(enqueued) == 2
    assert enqueued[0] == enqueued[1]

    async with test_session_factory() as session:
        jobs = (
            await session.execute(
                select(UcRecordingJob).where(UcRecordingJob.provider == "teams_compliance")
            )
        ).scalars().all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.tenant_id == test_tenant.id
        assert job.external_call_id == "MSpkLTk5"
        assert job.recording_id == "RG9jLTk5"
        assert job.state == "pending"
        assert job.payload["odata_id"] == (
            "communications/onlineMeetings/MSpkLTk5/recordings/RG9jLTk5"
        )


async def test_recording_job_duplicate_delivery_does_not_reenqueue_when_in_progress(
    teams_test_client,
    test_tenant,
    test_session_factory,
    seeded_teams_integration,
    monkeypatch,
):
    from backend.app.models import UcRecordingJob
    from backend.app.services.teams_recording import ingest as ingest_module

    enqueued = []
    monkeypatch.setattr(
        ingest_module, "_enqueue_fetch", lambda jid: enqueued.append(str(jid))
    )

    payload = load_fixture("notification_recording_created.json")
    payload["value"] = payload["value"][:1]  # single entry this time
    resp = await teams_test_client.post("/api/v1/teams/notification", json=payload)
    assert resp.status_code == 202

    async with test_session_factory() as session:
        job = (
            await session.execute(
                select(UcRecordingJob).where(UcRecordingJob.provider == "teams_compliance")
            )
        ).scalar_one()
        job.state = "in_progress"
        await session.commit()

    enqueued.clear()
    resp2 = await teams_test_client.post("/api/v1/teams/notification", json=payload)
    assert resp2.status_code == 202
    assert enqueued == []

    async with test_session_factory() as session:
        jobs = (
            await session.execute(
                select(UcRecordingJob).where(UcRecordingJob.provider == "teams_compliance")
            )
        ).scalars().all()
        assert len(jobs) == 1


async def test_notification_unknown_aad_tenant_is_skipped_gracefully(
    teams_test_client, test_session_factory
):
    """No teams_compliance Integration exists at all — every entry
    should resolve to the graceful skip, never a 500."""
    from backend.app.models import TeamsCallRecord, UcRecordingJob

    payload = load_fixture("notification_call_record.json")
    resp = await teams_test_client.post("/api/v1/teams/notification", json=payload)
    assert resp.status_code == 202
    assert resp.json()["results"] == [{"action": "skipped_unknown_tenant"}]

    async with test_session_factory() as session:
        assert (await session.execute(select(TeamsCallRecord))).scalars().all() == []
        assert (await session.execute(select(UcRecordingJob))).scalars().all() == []


# ── clientState enforcement when TEAMS_GRAPH_CLIENT_STATE is set ───────


async def test_notification_rejects_bad_client_state_when_configured(
    teams_test_client, monkeypatch
):
    from backend.app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("TEAMS_GRAPH_CLIENT_STATE", "scaffold-shared-secret")
    try:
        payload = load_fixture("notification_bad_client_state.json")
        resp = await teams_test_client.post("/api/v1/teams/notification", json=payload)
        assert resp.status_code == 400
        assert "clientState" in resp.json()["error"]
    finally:
        get_settings.cache_clear()


# ── TeamsComplianceProvider (UC adapter) ─────────────────────────────────


async def test_teams_compliance_provider_verify_webhook_extracts_recording_event():
    from backend.app.services.telephony.uc.base import get_provider

    provider = get_provider("teams_compliance")
    body = json.dumps(load_fixture("notification_recording_created.json")).encode()
    event = await provider.verify_webhook(
        headers={}, body=body, signing_secret="scaffold-shared-secret"
    )
    assert event.external_call_id == "MSpkLTk5"
    assert event.recording_id == "RG9jLTk5"


async def test_teams_compliance_provider_verify_webhook_rejects_call_record_only_batch():
    from backend.app.services.telephony.uc.base import WebhookVerificationError, get_provider

    provider = get_provider("teams_compliance")
    body = json.dumps(load_fixture("notification_call_record.json")).encode()
    with pytest.raises(WebhookVerificationError):
        await provider.verify_webhook(
            headers={}, body=body, signing_secret="scaffold-shared-secret"
        )


@respx.mock
async def test_teams_compliance_provider_fetch_recording_uses_app_only_bearer(monkeypatch):
    from backend.app.services.telephony.uc.base import UCWebhookEvent, get_provider
    from backend.app.services.telephony.uc import teams_compliance as tc_module

    class _FakeAuth:
        def authorization_header(self) -> str:
            return "Bearer fixture-app-only-token"

    monkeypatch.setattr(tc_module, "get_graph_app_auth", lambda: _FakeAuth())

    audio = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + b"\xff\xfb\x90\x64"
    route = respx.get(
        "https://graph.microsoft.com/v1.0/communications/onlineMeetings/"
        "MSpkLTk5/recordings/RG9jLTk5/content"
    ).mock(return_value=Response(200, content=audio, headers={"content-type": "audio/mpeg"}))

    provider = get_provider("teams_compliance")
    event = UCWebhookEvent(
        provider="teams_compliance",
        external_call_id="MSpkLTk5",
        recording_id="RG9jLTk5",
        raw={"odata_id": "communications/onlineMeetings/MSpkLTk5/recordings/RG9jLTk5"},
    )
    fetched = await provider.fetch_recording(access_token="unused-placeholder", event=event)
    assert fetched.audio_bytes == audio
    assert fetched.content_type == "audio/mpeg"
    assert route.calls.last.request.headers["authorization"] == "Bearer fixture-app-only-token"
