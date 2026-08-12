"""Tests for the Ask LINDA Tier-1 read tools (linda_reads).

The interesting properties are the ones that could go wrong quietly:
tenant scoping, the profile RBAC gates being the *same* ones the REST
router enforces, and the delegation seams not drifting from what they
delegate to.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import linda_reads


def _ctx(session, tenant, user=None):
    from backend.app.services.linda_agent import AgentContext

    return AgentContext(
        db=session, tenant=tenant, user=user, conversation_id=uuid.uuid4()
    )


async def _seed_account(session, tenant):
    from backend.app.models import (
        Contact,
        Customer,
        CustomerCommitment,
        CustomerConcern,
        Interaction,
    )

    customer = Customer(
        tenant_id=tenant.id,
        name="Acme Corporation",
        domain="acme.com",
        health_score=61.5,
        renewal_date=date(2026, 11, 1),
        onboarding_status="completed",
    )
    session.add(customer)
    await session.commit()
    await session.refresh(customer)

    session.add_all([
        CustomerConcern(
            tenant_id=tenant.id, customer_id=customer.id, topic="pricing",
            description="Pushing back on the 2026 uplift", severity="high",
            status="active",
            first_seen_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            status_changed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        CustomerConcern(
            tenant_id=tenant.id, customer_id=customer.id, topic="onboarding",
            description="Resolved months ago", severity="low", status="resolved",
            first_seen_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
            status_changed_at=datetime(2026, 3, 6, tzinfo=timezone.utc),
        ),
        CustomerCommitment(
            tenant_id=tenant.id, customer_id=customer.id,
            description="Send the security questionnaire",
            due_date=date(2026, 8, 20), status="open",
        ),
        CustomerCommitment(
            tenant_id=tenant.id, customer_id=customer.id,
            description="Already delivered", status="met",
        ),
        Contact(
            tenant_id=tenant.id, customer_id=customer.id, name="Dana Reyes",
            email="dana@acme.com", role="champion", interaction_count=7,
        ),
        Interaction(
            tenant_id=tenant.id, customer_id=customer.id, channel="voice",
            title="QBR", insights={"summary": "Went well", "sentiment_overall": "positive"},
        ),
    ])
    await session.commit()
    return customer


# ── get_customer_360 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_customer_360_returns_only_open_concerns_and_commitments(
    test_session, test_tenant
):
    customer = await _seed_account(test_session, test_tenant)

    out = await linda_reads.customer_360(test_session, test_tenant, str(customer.id))

    assert out["customer"]["name"] == "Acme Corporation"
    assert out["customer"]["health_score"] == 61.5
    assert [c["topic"] for c in out["open_concerns"]] == ["pricing"]
    assert [c["description"] for c in out["open_commitments"]] == [
        "Send the security questionnaire"
    ]
    assert out["contacts"][0]["email"] == "dana@acme.com"
    assert out["recent_interactions"][0]["title"] == "QBR"


@pytest.mark.asyncio
async def test_customer_360_flags_the_health_score_as_stored_not_live(
    test_session, test_tenant
):
    """The compute path is sync/Celery-shaped, so this reads the persisted
    value. Saying so keeps the model from presenting it as of-this-second."""
    customer = await _seed_account(test_session, test_tenant)

    out = await linda_reads.customer_360(test_session, test_tenant, str(customer.id))

    assert "not live" in out["customer"]["health_score_note"]


@pytest.mark.asyncio
async def test_customer_360_is_tenant_scoped(test_session_factory, test_tenant):
    from backend.app.models import Customer, Tenant

    async with test_session_factory() as session:
        other = Tenant(name="Other", slug="o-%s" % uuid.uuid4().hex[:6])
        session.add(other)
        await session.commit()
        await session.refresh(other)
        foreign = Customer(tenant_id=other.id, name="Not Yours")
        session.add(foreign)
        await session.commit()
        await session.refresh(foreign)
        foreign_id = foreign.id

    async with test_session_factory() as session:
        out = await linda_reads.customer_360(session, test_tenant, str(foreign_id))

    assert out == {"error": "customer not found"}


@pytest.mark.asyncio
async def test_customer_360_rejects_a_bad_id_without_raising(test_session, test_tenant):
    out = await linda_reads.customer_360(test_session, test_tenant, "not-a-uuid")
    assert out == {"error": "invalid customer_id"}


# ── search_knowledge_base ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_knowledge_base_shapes_hits_with_citable_titles(
    test_session, test_tenant
):
    from backend.app.models import KBDocument

    doc = KBDocument(
        tenant_id=test_tenant.id,
        title="Refund policy",
        content="Refunds within 30 days, excluding annual plans. " + "x" * 2000,
        source_type="upload",
    )
    test_session.add(doc)
    await test_session.commit()
    await test_session.refresh(doc)

    with patch(
        "backend.app.services.kb_document_retrieval.retrieve",
        new=AsyncMock(return_value=[(doc, 0.87)]),
    ):
        out = await linda_reads.search_knowledge_base(
            test_session, test_tenant, "refund policy"
        )

    assert out["count"] == 1
    hit = out["documents"][0]
    assert hit["title"] == "Refund policy"
    assert hit["score"] == 0.87
    assert hit["excerpt"].startswith("Refunds within 30 days")
    assert len(hit["excerpt"]) <= 800


@pytest.mark.asyncio
async def test_search_knowledge_base_empty_query_short_circuits(
    test_session, test_tenant
):
    with patch(
        "backend.app.services.kb_document_retrieval.retrieve", new=AsyncMock()
    ) as retrieve:
        out = await linda_reads.search_knowledge_base(test_session, test_tenant, "  ")
    assert out == {"documents": [], "count": 0}
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_knowledge_base_failure_returns_error_not_exception(
    test_session, test_tenant
):
    with patch(
        "backend.app.services.kb_document_retrieval.retrieve",
        new=AsyncMock(side_effect=RuntimeError("qdrant down")),
    ):
        out = await linda_reads.search_knowledge_base(test_session, test_tenant, "policy")
    assert "error" in out


def test_knowledge_base_results_are_never_model_condensed():
    """KB excerpts are the grounding for policy answers — a condense pass
    that drops a qualifying clause manufactures a confident wrong policy."""
    from backend.app.services.linda_context import CONDENSABLE

    assert "search_knowledge_base" not in CONDENSABLE
    assert "get_customer_360" not in CONDENSABLE
    assert "get_team_metrics" not in CONDENSABLE
    assert "get_profile" not in CONDENSABLE


# ── get_profile (RBAC) ─────────────────────────────────────────────────────


async def _seed_profiles(session, tenant):
    from backend.app.models import AgentProfile, BusinessProfile, User

    manager = User(tenant_id=tenant.id, email="mgr@co.com", name="Mo", role="manager")
    session.add(manager)
    await session.commit()
    await session.refresh(manager)

    agent = User(
        tenant_id=tenant.id, email="rep@co.com", name="Ray", role="agent",
        manager_id=manager.id,
    )
    stranger = User(tenant_id=tenant.id, email="other@co.com", name="Sam", role="agent")
    session.add_all([agent, stranger])
    await session.commit()
    await session.refresh(agent)
    await session.refresh(stranger)

    session.add_all([
        AgentProfile(
            tenant_id=tenant.id, agent_id=agent.id, version=1, confidence=0.7,
            profile={"summary": "Strong discovery", "metrics": {"calls": 12},
                     "recommendations": []},
            top_factors=[],
        ),
        AgentProfile(
            tenant_id=tenant.id, agent_id=agent.id, version=2, confidence=0.8,
            profile={"summary": "Latest read", "metrics": {"calls": 20},
                     "recommendations": []},
            top_factors=[],
        ),
        BusinessProfile(
            tenant_id=tenant.id, business_tenant_id=tenant.id, version=1, confidence=0.9,
            profile={"summary": "Business-wide", "metrics": {}, "recommendations": []},
            top_factors=[],
        ),
    ])
    await session.commit()
    return manager, agent, stranger


@pytest.mark.asyncio
async def test_get_profile_returns_the_latest_version(test_session, test_tenant):
    _, agent, _ = await _seed_profiles(test_session, test_tenant)

    out = await linda_reads.get_profile(
        test_session, test_tenant, agent, "agent", str(agent.id)
    )

    assert out["kind"] == "agent"
    assert out["profile"]["version"] == 2
    assert out["profile"]["summary"] == "Latest read"


@pytest.mark.asyncio
async def test_agent_cannot_read_another_agents_profile(test_session, test_tenant):
    """The chat tool must enforce the same role scoping api/profiles.py does
    — the other chat reads are tenant-wide, this surface is not."""
    _, agent, stranger = await _seed_profiles(test_session, test_tenant)

    out = await linda_reads.get_profile(
        test_session, test_tenant, stranger, "agent", str(agent.id)
    )

    assert "access" in out["error"].lower()
    assert "profile" not in out


@pytest.mark.asyncio
async def test_manager_can_read_their_reports_profile(test_session, test_tenant):
    manager, agent, _ = await _seed_profiles(test_session, test_tenant)

    out = await linda_reads.get_profile(
        test_session, test_tenant, manager, "agent", str(agent.id)
    )

    assert out["profile"]["summary"] == "Latest read"


@pytest.mark.asyncio
async def test_business_profile_is_admin_only(test_session, test_tenant):
    manager, agent, _ = await _seed_profiles(test_session, test_tenant)

    for user in (agent, manager):
        out = await linda_reads.get_profile(test_session, test_tenant, user, "business")
        assert "access" in out["error"].lower(), user.role


@pytest.mark.asyncio
async def test_api_key_caller_is_treated_as_tenant_admin(test_session, test_tenant):
    """``AgentContext.user`` is None for tenant-API-key callers. auth.py
    documents API keys as tenant-wide credentials and builds their
    principal with role='admin'; chat must match, or the same key that can
    GET /profiles/business directly would be refused through Linda."""
    await _seed_profiles(test_session, test_tenant)

    out = await linda_reads.get_profile(test_session, test_tenant, None, "business")

    assert out["profile"]["summary"] == "Business-wide"


@pytest.mark.asyncio
async def test_get_profile_denial_does_not_reveal_whether_the_entity_exists(
    test_session, test_tenant
):
    """Same message for "not allowed" and "no such agent", or the tool
    becomes an existence oracle for entities the caller can't see."""
    _, agent, stranger = await _seed_profiles(test_session, test_tenant)

    denied = await linda_reads.get_profile(
        test_session, test_tenant, stranger, "agent", str(agent.id)
    )
    nonexistent = await linda_reads.get_profile(
        test_session, test_tenant, stranger, "agent", str(uuid.uuid4())
    )

    assert denied["error"] == nonexistent["error"]


@pytest.mark.asyncio
async def test_get_profile_unknown_kind_and_missing_id_are_reported(
    test_session, test_tenant
):
    _, agent, _ = await _seed_profiles(test_session, test_tenant)

    bad_kind = await linda_reads.get_profile(
        test_session, test_tenant, agent, "wizard", str(agent.id)
    )
    assert "unknown profile kind" in bad_kind["error"]

    no_id = await linda_reads.get_profile(test_session, test_tenant, agent, "agent")
    assert "entity_id" in no_id["error"]
    assert "resolve_entity" in no_id["error"]


@pytest.mark.asyncio
async def test_get_profile_missing_row_says_so(test_session, test_tenant):
    manager, agent, _ = await _seed_profiles(test_session, test_tenant)

    out = await linda_reads.get_profile(
        test_session, test_tenant, manager, "manager", str(manager.id)
    )

    assert "no manager profile" in out["error"]


# ── get_team_metrics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_team_metrics_delegates_to_the_analytics_dashboard(
    test_session, test_tenant
):
    """One SQL implementation, so a chat answer and the SPA dashboard can
    never quote different numbers for the same period."""
    summary = SimpleNamespace(
        model_dump=lambda: {"total_interactions": 42, "avg_sentiment_score": 0.31}
    )
    with patch(
        "backend.app.api.analytics.dashboard", new=AsyncMock(return_value=summary)
    ) as dash:
        out = await linda_reads.team_metrics(test_session, test_tenant, "7d")

    assert out["total_interactions"] == 42
    assert out["period"] == "7d"
    assert dash.await_args.kwargs["period"] == "7d"
    assert dash.await_args.kwargs["tenant"] is test_tenant


@pytest.mark.asyncio
async def test_team_metrics_rejects_an_unsupported_period_before_querying(
    test_session, test_tenant
):
    with patch("backend.app.api.analytics.dashboard", new=AsyncMock()) as dash:
        out = await linda_reads.team_metrics(test_session, test_tenant, "all-time")
    assert "unknown period" in out["error"]
    dash.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_metrics_failure_returns_error_not_exception(
    test_session, test_tenant
):
    with patch(
        "backend.app.api.analytics.dashboard",
        new=AsyncMock(side_effect=RuntimeError("pg down")),
    ):
        out = await linda_reads.team_metrics(test_session, test_tenant)
    assert "error" in out


# ── dispatch wiring ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tier1_reads_are_reachable_through_dispatch_tool(
    test_session, test_tenant
):
    from backend.app.services.linda_agent import dispatch_tool

    customer = await _seed_account(test_session, test_tenant)
    ctx = _ctx(test_session, test_tenant)

    out = await dispatch_tool(ctx, "get_customer_360", {"customer_id": str(customer.id)})
    assert out["customer"]["name"] == "Acme Corporation"

    with patch(
        "backend.app.api.analytics.dashboard",
        new=AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {"total_interactions": 1})),
    ):
        metrics = await dispatch_tool(ctx, "get_team_metrics", {})
    assert metrics["period"] == "30d"
