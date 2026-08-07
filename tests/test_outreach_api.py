"""Integration tests for the cold-outreach API (SQLite fixtures).

Mounts only the outreach router (same pattern as tests/db_fixtures.py's
test_app) with get_db / get_current_tenant / get_current_principal
overridden. Webhook emission and Celery enqueues are best-effort no-ops
in this environment.
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


VALID_CONFIG = {
    "template": {
        "subject": "Quick question about {business_name}",
        "body": "Hi — saw you run {business_name}. {hook}",
        "sender_name": "Davis Fox",
        "sender_business": "Flex",
        "physical_address": "123 Main St, Nashville, TN 37201",
    },
    "daily_limit": 10,
    "max_touches": 3,
    "mode": "review",
}


def _rows(n: int = 3):
    return [
        {
            "business_name": f"Gym {i}",
            "website": f"https://www.gym-{i}.com/",
            "city": "Nashville",
            "state": "TN",
            "segment": "boutique",
            "current_software": "MindBody",
            "hook": f"Hook {i}",
            "contact": {"name": f"Owner {i}", "email": f"owner{i}@gym-{i}.com"},
            "source": "sweep-2026-07",
        }
        for i in range(n)
    ]


@pytest_asyncio.fixture
async def outreach_app(test_session_factory, test_tenant):
    from fastapi import FastAPI

    from backend.app.api.contacts import router as contacts_router
    from backend.app.api.outreach import router as outreach_router
    from backend.app.auth import (
        AuthPrincipal,
        get_current_principal,
        get_current_tenant,
    )
    from backend.app.db import get_db
    from backend.app.models import Tenant
    from sqlalchemy import select

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _get_tenant() -> Tenant:
        async with test_session_factory() as s:
            return (
                await s.execute(select(Tenant).where(Tenant.id == test_tenant.id))
            ).scalar_one()

    async def _override_get_principal():
        return AuthPrincipal(
            tenant=await _get_tenant(),
            user=None,
            role="admin",
            source="api_key",
            scopes=["campaigns:write", "contacts:write"],
        )

    app = FastAPI()
    app.include_router(outreach_router, prefix="/api/v1")
    app.include_router(contacts_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_tenant] = _get_tenant
    app.dependency_overrides[get_current_principal] = _override_get_principal
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(outreach_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=outreach_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Import ──────────────────────────────────────────────────────────────


async def test_import_creates_prospects_and_contacts(client, test_session_factory, test_tenant):
    resp = await client.post(
        "/api/v1/prospects/import", json={"prospects": _rows(3)}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 3
    assert body["updated"] == 0
    assert body["errors"] == []
    assert all(r["created"] for r in body["prospects"])
    assert body["prospects"][0]["domain"] == "gym-0.com"
    assert body["prospects"][0]["pipeline_status"] == "new"
    assert body["prospects"][0]["contact_id"] is not None

    from backend.app.models import Contact, Customer
    from sqlalchemy import select

    async with test_session_factory() as s:
        customers = (
            (await s.execute(
                select(Customer).where(
                    Customer.tenant_id == test_tenant.id,
                    Customer.pipeline_status.is_not(None),
                )
            )).scalars().all()
        )
        assert len(customers) == 3
        meta = customers[0].metadata_["outreach"]
        assert meta["segment"] == "boutique"
        assert meta["source"] == "sweep-2026-07"
        contact = (
            (await s.execute(
                select(Contact).where(Contact.customer_id == customers[0].id)
            )).scalars().first()
        )
        assert contact is not None and contact.email.startswith("owner")


async def test_import_is_idempotent_on_domain(client):
    first = await client.post("/api/v1/prospects/import", json={"prospects": _rows(2)})
    assert first.status_code == 200

    # Same domains, different URL forms + tweaked metadata → update, not dup.
    rows = _rows(2)
    rows[0]["website"] = "GYM-0.COM/pricing"
    rows[0]["hook"] = "Updated hook"
    second = await client.post("/api/v1/prospects/import", json={"prospects": rows})
    body = second.json()
    assert body["created"] == 0
    assert body["updated"] == 2
    ids_first = {r["prospect_id"] for r in first.json()["prospects"]}
    ids_second = {r["prospect_id"] for r in body["prospects"]}
    assert ids_first == ids_second

    listing = (await client.get("/api/v1/prospects")).json()
    assert listing["total"] == 2
    hooks = {i["hook"] for i in listing["items"]}
    assert "Updated hook" in hooks


async def test_import_same_name_different_domains_creates_two_prospects(client):
    """Two distinct businesses sharing a name must never merge when both
    rows carry (different) domains — regression for the 2026-07-14 launch
    import that attached the Katy TX contact to the Sioux Falls gym."""
    rows = [
        {
            "business_name": "Results Personal Training",
            "website": "https://resultspersonaltraining.com",
            "city": "Sioux Falls",
            "state": "SD",
            "contact": {"email": "info@resultspersonaltraining.com"},
        },
        {
            "business_name": "Results Personal Training",
            "website": "https://www.personaltrainersinkaty.com/",
            "city": "Katy",
            "state": "TX",
            "contact": {"email": "info@personaltrainersinkaty.com"},
        },
    ]
    resp = await client.post("/api/v1/prospects/import", json={"prospects": rows})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    p0, p1 = body["prospects"]
    assert p0["prospect_id"] != p1["prospect_id"]
    assert {p0["domain"], p1["domain"]} == {
        "resultspersonaltraining.com",
        "personaltrainersinkaty.com",
    }

    # Each prospect keeps its own contact + geo, no cross-attachment.
    listing = (await client.get("/api/v1/prospects")).json()
    by_domain = {i["domain"]: i for i in listing["items"]}
    assert (
        by_domain["resultspersonaltraining.com"]["primary_contact"]["email"]
        == "info@resultspersonaltraining.com"
    )
    assert by_domain["resultspersonaltraining.com"]["state"] == "SD"
    assert (
        by_domain["personaltrainersinkaty.com"]["primary_contact"]["email"]
        == "info@personaltrainersinkaty.com"
    )
    assert by_domain["personaltrainersinkaty.com"]["state"] == "TX"


async def test_import_same_name_without_domain_still_merges(client):
    """Name fallback is preserved for rows with no parseable domain."""
    first = (
        await client.post(
            "/api/v1/prospects/import",
            json={
                "prospects": [
                    {
                        "business_name": "Personally Fit",
                        "website": "https://personallyfit.com",
                        "contact": {"email": "owner@personallyfit.com"},
                    }
                ]
            },
        )
    ).json()
    assert first["created"] == 1
    pid = first["prospects"][0]["prospect_id"]

    second = (
        await client.post(
            "/api/v1/prospects/import",
            json={
                "prospects": [
                    {
                        "business_name": "Personally Fit",
                        "website": None,
                        "hook": "Fresh hook",
                    }
                ]
            },
        )
    ).json()
    assert second["created"] == 0
    assert second["updated"] == 1
    assert second["prospects"][0]["prospect_id"] == pid


async def test_reimport_never_resets_status_or_resurrects_dnc(client):
    imported = (
        await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    ).json()["prospects"][0]
    pid = imported["prospect_id"]

    # Advance manually, then opt out.
    await client.patch(f"/api/v1/prospects/{pid}", json={"pipeline_status": "demo"})
    await client.post(f"/api/v1/prospects/{pid}/opt-out")

    again = (
        await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    ).json()
    assert again["updated"] == 1
    prospect = (await client.get(f"/api/v1/prospects/{pid}")).json()
    assert prospect["pipeline_status"] == "do_not_contact"
    assert prospect["do_not_contact"] is True


async def test_import_row_cap():
    from backend.app.api.outreach import ProspectImportRequest
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        ProspectImportRequest(prospects=_rows(1) * 501)


# ── Prospect list / patch ──────────────────────────────────────────────


async def test_list_prospects_filters(client):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(3)})
    pid = (await client.get("/api/v1/prospects")).json()["items"][0]["prospect_id"]
    await client.patch(f"/api/v1/prospects/{pid}", json={"pipeline_status": "demo"})

    demo = (await client.get("/api/v1/prospects", params={"status": "demo"})).json()
    assert demo["total"] == 1
    assert demo["items"][0]["prospect_id"] == pid

    q = (await client.get("/api/v1/prospects", params={"q": "gym-1"})).json()
    assert q["total"] == 1

    bad = await client.get("/api/v1/prospects", params={"status": "nonsense"})
    assert bad.status_code == 422


async def test_manual_dnc_halts_members(client, test_session_factory, test_tenant):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    pid = (await client.get("/api/v1/prospects")).json()["items"][0]["prospect_id"]

    created = await client.post(
        "/api/v1/outreach/campaigns",
        json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": [pid]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["member_states"] == {"draft_pending": 1}

    resp = await client.patch(
        f"/api/v1/prospects/{pid}", json={"do_not_contact": True}
    )
    assert resp.status_code == 200
    assert resp.json()["pipeline_status"] == "do_not_contact"

    from backend.app.models import OutreachMember
    from sqlalchemy import select

    async with test_session_factory() as s:
        member = (
            (await s.execute(
                select(OutreachMember).where(OutreachMember.tenant_id == test_tenant.id)
            )).scalars().one()
        )
        assert member.state == "halted"
        assert member.halt_reason == "manual_dnc"


# ── Campaigns ──────────────────────────────────────────────────────────


async def test_campaign_create_validates_can_spam(client):
    bad = dict(VALID_CONFIG, template={**VALID_CONFIG["template"]})
    del bad["template"]["physical_address"]
    resp = await client.post(
        "/api/v1/outreach/campaigns", json={"name": "Bad", "config": bad}
    )
    assert resp.status_code == 422


async def test_campaign_enroll_skips_no_email_and_dnc(client):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(2)})
    items = (await client.get("/api/v1/prospects")).json()["items"]
    ids = [i["prospect_id"] for i in items]

    # No-contact prospect: import a row without a contact email.
    bare = {"business_name": "No Contact Gym", "website": "nocontact.com"}
    extra = (
        await client.post("/api/v1/prospects/import", json={"prospects": [bare]})
    ).json()["prospects"][0]["prospect_id"]
    # DNC prospect
    await client.post(f"/api/v1/prospects/{ids[1]}/opt-out")

    resp = await client.post(
        "/api/v1/outreach/campaigns",
        json={
            "name": "Sweep",
            "config": VALID_CONFIG,
            "prospect_ids": ids + [extra, str(uuid.uuid4())],
        },
    )
    body = resp.json()
    assert body["member_states"] == {"draft_pending": 1}
    reasons = {s["prospect_id"]: s["reason"] for s in body["skipped"]}
    assert reasons[ids[1]] == "do_not_contact"
    assert reasons[extra] == "no_contact_email"
    assert "not_found" in reasons.values()


async def test_draft_review_approve_flow(client, test_session_factory, test_tenant):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(2)})
    ids = [i["prospect_id"] for i in (await client.get("/api/v1/prospects")).json()["items"]]
    campaign_id = (
        await client.post(
            "/api/v1/outreach/campaigns",
            json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": ids},
        )
    ).json()["id"]

    # Simulate the Celery draft generator having produced drafts.
    from backend.app.models import OutreachMember
    from sqlalchemy import select

    async with test_session_factory() as s:
        members = (
            (await s.execute(
                select(OutreachMember).where(OutreachMember.tenant_id == test_tenant.id)
            )).scalars().all()
        )
        for m in members:
            m.draft_subject = "Hello"
            m.draft_body = "Personalized body"
            m.draft_status = "ready"
            m.state = "needs_approval"
        await s.commit()
        member_ids = [str(m.id) for m in members]

    # Individual edit + approve in one PATCH.
    one = await client.patch(
        f"/api/v1/outreach/members/{member_ids[0]}",
        json={"draft_body": "Edited body", "action": "approve"},
    )
    assert one.status_code == 200, one.text
    assert one.json()["state"] == "queued"
    assert one.json()["draft_status"] == "approved"
    assert one.json()["draft_body"] == "Edited body"

    # Bulk approve the rest.
    bulk = await client.post(
        f"/api/v1/outreach/campaigns/{campaign_id}/approve-drafts",
        json={"all": True},
    )
    assert bulk.json()["approved"] == 1

    listing = await client.get(
        f"/api/v1/outreach/campaigns/{campaign_id}/members",
        params={"state": "queued"},
    )
    assert listing.json()["total"] == 2

    # Reject flow: knock one back to regeneration.
    rej = await client.patch(
        f"/api/v1/outreach/members/{member_ids[1]}", json={"action": "reject"}
    )
    assert rej.json()["state"] == "draft_pending"
    assert rej.json()["draft_subject"] is None


async def test_patch_campaign_updates_fields_on_live_campaign(client):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    pid = (await client.get("/api/v1/prospects")).json()["items"][0]["prospect_id"]
    created = (
        await client.post(
            "/api/v1/outreach/campaigns",
            json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": [pid]},
        )
    ).json()
    campaign_id = created["id"]
    # VALID_CONFIG carries no steps → the two defaults (offsets 0 and 4).
    assert [s["offset_days"] for s in created["config"]["steps"]] == [0, 4]

    resp = await client.patch(
        f"/api/v1/outreach/campaigns/{campaign_id}",
        json={
            "name": "Sweep v2",
            "config": {
                "steps": [{}, {"guidance": "Bump with the new hook."}],
                "template": {"subject": "New subject for {business_name}"},
                "send_window": {"start_hour": 8, "end_hour": 12},
                "daily_limit": 5,
                "mode": "auto",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Sweep v2"
    config = body["config"]
    # Same step count/offsets; only guidance changed.
    assert [s["offset_days"] for s in config["steps"]] == [0, 4]
    assert config["steps"][1]["guidance"] == "Bump with the new hook."
    assert config["template"]["subject"] == "New subject for {business_name}"
    # Untouched template fields survive the merge.
    assert config["template"]["body"] == VALID_CONFIG["template"]["body"]
    assert config["template"]["sender_name"] == "Davis Fox"
    assert config["send_window"]["start_hour"] == 8
    assert config["send_window"]["timezone"] == "America/New_York"
    assert config["daily_limit"] == 5
    assert config["mode"] == "auto"

    # GET reflects the same merged config.
    fetched = (await client.get(f"/api/v1/outreach/campaigns/{campaign_id}")).json()
    assert fetched["config"] == config
    assert fetched["name"] == "Sweep v2"


async def test_patch_campaign_locks_timing_once_members_in_flight(
    client, test_session_factory, test_tenant
):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    pid = (await client.get("/api/v1/prospects")).json()["items"][0]["prospect_id"]
    campaign_id = (
        await client.post(
            "/api/v1/outreach/campaigns",
            json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": [pid]},
        )
    ).json()["id"]

    # All members still draft_pending → timing is free to change.
    grow = await client.patch(
        f"/api/v1/outreach/campaigns/{campaign_id}",
        json={"config": {"steps": [{}, {"offset_days": 7}, {"offset_days": 14}]}},
    )
    assert grow.status_code == 200, grow.text
    assert [s["offset_days"] for s in grow.json()["config"]["steps"]] == [0, 7, 14]

    # A member advances (draft generated) → timing locks.
    from backend.app.models import OutreachMember
    from sqlalchemy import select

    async with test_session_factory() as s:
        member = (
            (await s.execute(
                select(OutreachMember).where(OutreachMember.tenant_id == test_tenant.id)
            )).scalars().one()
        )
        member.draft_subject = "Hello"
        member.draft_body = "Body"
        member.draft_status = "ready"
        member.state = "needs_approval"
        await s.commit()

    offset_change = await client.patch(
        f"/api/v1/outreach/campaigns/{campaign_id}",
        json={"config": {"steps": [{}, {"offset_days": 10}, {}]}},
    )
    assert offset_change.status_code == 422
    assert "in flight" in offset_change.json()["detail"]

    count_change = await client.patch(
        f"/api/v1/outreach/campaigns/{campaign_id}",
        json={"config": {"steps": [{}, {}]}},
    )
    assert count_change.status_code == 422

    # Guidance-only (same count, same offsets) still lands.
    guidance_only = await client.patch(
        f"/api/v1/outreach/campaigns/{campaign_id}",
        json={"config": {"steps": [{}, {"guidance": "Short bump."}, {}]}},
    )
    assert guidance_only.status_code == 200, guidance_only.text
    steps = guidance_only.json()["config"]["steps"]
    assert [s["offset_days"] for s in steps] == [0, 7, 14]
    assert steps[1]["guidance"] == "Short bump."


async def test_patch_campaign_rejects_bad_config(client):
    campaign_id = (
        await client.post(
            "/api/v1/outreach/campaigns", json={"name": "Sweep", "config": VALID_CONFIG}
        )
    ).json()["id"]

    bad_mode = await client.patch(
        f"/api/v1/outreach/campaigns/{campaign_id}",
        json={"config": {"mode": "yolo"}},
    )
    assert bad_mode.status_code == 422

    # Fields outside the PATCHable surface are rejected, not merged.
    not_patchable = await client.patch(
        f"/api/v1/outreach/campaigns/{campaign_id}",
        json={"config": {"track_clicks": True}},
    )
    assert not_patchable.status_code == 422


async def test_member_halt_removes_from_review_and_bulk_approve(
    client, test_session_factory, test_tenant
):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(2)})
    ids = [i["prospect_id"] for i in (await client.get("/api/v1/prospects")).json()["items"]]
    campaign_id = (
        await client.post(
            "/api/v1/outreach/campaigns",
            json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": ids},
        )
    ).json()["id"]

    from backend.app.models import OutreachMember
    from sqlalchemy import select

    async with test_session_factory() as s:
        members = (
            (await s.execute(
                select(OutreachMember).where(OutreachMember.tenant_id == test_tenant.id)
            )).scalars().all()
        )
        for m in members:
            m.draft_subject = "Hello"
            m.draft_body = "Personalized body"
            m.draft_status = "ready"
            m.state = "needs_approval"
        await s.commit()
        member_ids = [str(m.id) for m in members]

    halted = await client.patch(
        f"/api/v1/outreach/members/{member_ids[0]}", json={"action": "halt"}
    )
    assert halted.status_code == 200, halted.text
    body = halted.json()
    assert body["state"] == "halted"
    assert body["halt_reason"] == "removed_manually"
    assert body["next_send_at"] is None
    # The unsent draft is gone with it.
    assert body["draft_subject"] is None
    assert body["draft_body"] is None
    assert body["draft_status"] is None

    # Gone from the review inbox…
    inbox = await client.get(
        f"/api/v1/outreach/campaigns/{campaign_id}/members",
        params={"state": "needs_approval"},
    )
    assert inbox.json()["total"] == 1
    assert inbox.json()["items"][0]["id"] == member_ids[1]

    # …and bulk approve only touches the survivor.
    bulk = await client.post(
        f"/api/v1/outreach/campaigns/{campaign_id}/approve-drafts",
        json={"all": True},
    )
    assert bulk.json()["approved"] == 1


async def test_member_halt_allowed_from_in_sequence_but_not_terminal(
    client, test_session_factory, test_tenant
):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(2)})
    ids = [i["prospect_id"] for i in (await client.get("/api/v1/prospects")).json()["items"]]
    await client.post(
        "/api/v1/outreach/campaigns",
        json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": ids},
    )

    from backend.app.models import OutreachMember
    from sqlalchemy import select

    async with test_session_factory() as s:
        members = (
            (await s.execute(
                select(OutreachMember).where(OutreachMember.tenant_id == test_tenant.id)
            )).scalars().all()
        )
        # Mid-sequence member with a bump pending — the state PATCH's
        # draft-edit guard would 409 on, but halt must reach.
        members[0].state = "in_sequence"
        members[0].touches_sent = 1
        members[1].state = "replied"
        await s.commit()
        in_seq_id, replied_id = str(members[0].id), str(members[1].id)

    ok = await client.patch(
        f"/api/v1/outreach/members/{in_seq_id}", json={"action": "halt"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "halted"

    conflict = await client.patch(
        f"/api/v1/outreach/members/{replied_id}", json={"action": "halt"}
    )
    assert conflict.status_code == 409


async def test_activate_requires_email_integration(client):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    pid = (await client.get("/api/v1/prospects")).json()["items"][0]["prospect_id"]
    campaign_id = (
        await client.post(
            "/api/v1/outreach/campaigns",
            json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": [pid]},
        )
    ).json()["id"]

    resp = await client.post(f"/api/v1/outreach/campaigns/{campaign_id}/activate")
    assert resp.status_code == 400
    assert "integration" in resp.json()["detail"].lower()


async def test_activate_and_pause_with_integration(client, test_session_factory, test_tenant, monkeypatch):
    from backend.app.models import Integration

    async with test_session_factory() as s:
        s.add(
            Integration(
                tenant_id=test_tenant.id,
                provider="google",
                access_token="enc",
                refresh_token="enc",
            )
        )
        await s.commit()

    # The activate path may enqueue draft generation — no broker here.
    import backend.app.tasks as tasks_mod

    monkeypatch.setattr(
        tasks_mod.outreach_generate_drafts, "delay", lambda *a, **k: None
    )

    await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    pid = (await client.get("/api/v1/prospects")).json()["items"][0]["prospect_id"]
    campaign_id = (
        await client.post(
            "/api/v1/outreach/campaigns",
            json={"name": "Sweep", "config": VALID_CONFIG, "prospect_ids": [pid]},
        )
    ).json()["id"]

    up = await client.post(f"/api/v1/outreach/campaigns/{campaign_id}/activate")
    assert up.status_code == 200, up.text
    assert up.json()["status"] == "active"
    assert up.json()["quota"]["daily_limit"] == 10
    assert up.json()["quota"]["sent_today"] == 0

    down = await client.post(f"/api/v1/outreach/campaigns/{campaign_id}/pause")
    assert down.json()["status"] == "paused"


# ── Timeline ───────────────────────────────────────────────────────────


async def test_prospect_timeline_merges_sources(client, test_session_factory, test_tenant):
    await client.post("/api/v1/prospects/import", json={"prospects": _rows(1)})
    pid = (await client.get("/api/v1/prospects")).json()["items"][0]["prospect_id"]

    from backend.app.models import CustomerNote, Interaction

    async with test_session_factory() as s:
        s.add(
            Interaction(
                tenant_id=test_tenant.id,
                customer_id=uuid.UUID(pid),
                channel="email",
                direction="outbound",
                subject="Intro",
                raw_text="Hello there",
            )
        )
        s.add(
            CustomerNote(
                tenant_id=test_tenant.id,
                customer_id=uuid.UUID(pid),
                body="Owner prefers texts",
            )
        )
        await s.commit()

    tl = (await client.get(f"/api/v1/prospects/{pid}/timeline")).json()
    kinds = [e["kind"] for e in tl["entries"]]
    assert "interaction" in kinds
    assert "note" in kinds
    subj = [e for e in tl["entries"] if e["kind"] == "interaction"][0]
    assert subj["subject"] == "Intro"
    assert subj["direction"] == "outbound"


# ── Pipeline-status consistency (customers vs prospects) ───────────────


async def test_post_customers_creates_pipeline_row(client):
    """A customer created via POST /customers must appear on the pipeline
    immediately — regression for launch prospects that were enrolled in a
    campaign yet invisible on the board."""
    resp = await client.post(
        "/api/v1/customers",
        json={"name": "SFT Fitness", "domain": "sftfitness.com"},
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    detail = (await client.get(f"/api/v1/prospects/{pid}")).json()
    assert detail["pipeline_status"] == "new"
    assert detail["pipeline_status_changed_at"] is not None

    listing = (await client.get("/api/v1/prospects")).json()
    assert pid in {i["prospect_id"] for i in listing["items"]}


async def test_null_status_customer_heals_on_prospect_read(
    client, test_session_factory, test_tenant
):
    """Legacy customers with no pipeline_status (created before the eager
    row existed) are backfilled to "new" the first time the prospect API
    reads them — GET /prospects/{id} never returns pipeline_status: null."""
    from backend.app.models import Customer

    async with test_session_factory() as s:
        legacy = Customer(
            tenant_id=test_tenant.id, name="Raymer Strength", domain="raymer.com"
        )
        s.add(legacy)
        await s.commit()
        pid = str(legacy.id)

    # Invisible on the list before the heal…
    before = (await client.get("/api/v1/prospects")).json()
    assert pid not in {i["prospect_id"] for i in before["items"]}

    detail = (await client.get(f"/api/v1/prospects/{pid}")).json()
    assert detail["pipeline_status"] == "new"

    # …and the heal persists: the list now includes the row.
    after = (await client.get("/api/v1/prospects")).json()
    assert pid in {i["prospect_id"] for i in after["items"]}
