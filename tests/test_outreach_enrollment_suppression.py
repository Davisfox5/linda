"""Suppression gates on outreach enrollment.

Two cross-repo invariants live here, and both fail silently if broken —
nobody notices an email that shouldn't have been sent until the recipient
replies:

* a prospect carrying ``metadata.inbound`` asked to be contacted, and must
  never be swept into a cold sequence;
* when an external suppression source is registered, an *unreachable* source
  blocks enrollment rather than waving it through. "I couldn't check" is not
  "it's fine".

These drive ``_enroll_prospects`` directly rather than through HTTP: the
gates are the unit under test, and the endpoint wrapper adds only auth.
"""

import uuid

import pytest
import pytest_asyncio

from backend.app.services import mcp_tools


@pytest_asyncio.fixture
async def campaign_and_prospects(test_session_factory, test_tenant):
    """One outreach campaign plus two contactable prospects on one domain."""
    from backend.app.models import Campaign, Contact, Customer

    async with test_session_factory() as session:
        campaign = Campaign(
            tenant_id=test_tenant.id,
            name="Gyms sweep",
            channel="email",
            kind="outreach",
            status="draft",
            config={},
        )
        session.add(campaign)
        await session.flush()

        made = []
        for i in range(2):
            customer = Customer(
                tenant_id=test_tenant.id,
                name=f"Acme Gym {i}",
                domain="acmegym.com",
                pipeline_status="new",
            )
            session.add(customer)
            await session.flush()
            session.add(
                Contact(
                    tenant_id=test_tenant.id,
                    customer_id=customer.id,
                    email=f"owner{i}@acmegym.com",
                    name=f"Owner {i}",
                )
            )
            made.append(customer)
        await session.commit()
        return campaign.id, [c.id for c in made]


def _server_with_dnc():
    return mcp_tools.McpServer(
        integration_id=uuid.uuid4(),
        name="flex",
        endpoint="https://admin.example.com/api/linda-mcp",
        secret="k",
        tools=[{"name": "check_do_not_contact", "description": "", "input_schema": {}}],
    )


async def _enroll(session, tenant, campaign_id, prospect_ids):
    from backend.app.api.outreach import _enroll_prospects
    from backend.app.models import Campaign

    campaign = await session.get(Campaign, campaign_id)
    return await _enroll_prospects(session, tenant, campaign, list(prospect_ids))


def _reasons(skipped):
    return sorted(s.reason for s in skipped)


# ── No external source registered: unchanged behaviour ───────────────────


@pytest.mark.asyncio
async def test_without_an_external_source_enrollment_is_unchanged(
    test_session_factory, test_tenant, campaign_and_prospects
):
    campaign_id, prospect_ids = campaign_and_prospects
    async with test_session_factory() as session:
        added, skipped = await _enroll(session, test_tenant, campaign_id, prospect_ids)
    assert added == 2
    assert skipped == []


# ── Inbound leads ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_inbound_lead_is_never_enrolled(
    test_session_factory, test_tenant, campaign_and_prospects
):
    from backend.app.models import Customer

    campaign_id, prospect_ids = campaign_and_prospects
    async with test_session_factory() as session:
        customer = await session.get(Customer, prospect_ids[0])
        customer.metadata_ = {"source": "flex-console", "inbound": True}
        await session.commit()

    async with test_session_factory() as session:
        added, skipped = await _enroll(session, test_tenant, campaign_id, prospect_ids)

    assert added == 1
    assert _reasons(skipped) == ["inbound_lead"]


@pytest.mark.asyncio
async def test_a_non_inbound_metadata_flag_does_not_block(
    test_session_factory, test_tenant, campaign_and_prospects
):
    """Only the literal True blocks — a prospect merely carrying Flex
    metadata is still a cold-outreach target."""
    from backend.app.models import Customer

    campaign_id, prospect_ids = campaign_and_prospects
    async with test_session_factory() as session:
        customer = await session.get(Customer, prospect_ids[0])
        customer.metadata_ = {"source": "flex-console", "inbound": False}
        await session.commit()

    async with test_session_factory() as session:
        added, skipped = await _enroll(session, test_tenant, campaign_id, prospect_ids)

    assert added == 2
    assert skipped == []


# ── External suppression ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_suppression_blocks_enrollment(
    test_session_factory, test_tenant, campaign_and_prospects, monkeypatch
):
    campaign_id, prospect_ids = campaign_and_prospects
    monkeypatch.setattr(
        mcp_tools, "list_servers", _fake_list_servers([_server_with_dnc()])
    )

    async def blocked(server, domain):
        return mcp_tools.DncVerdict(
            available=True, blocked=True, reasons=["Already a paying customer."]
        )

    monkeypatch.setattr(mcp_tools, "check_do_not_contact", blocked)

    async with test_session_factory() as session:
        added, skipped = await _enroll(session, test_tenant, campaign_id, prospect_ids)

    assert added == 0
    assert _reasons(skipped) == ["external_do_not_contact"] * 2


@pytest.mark.asyncio
async def test_an_unreachable_source_fails_closed(
    test_session_factory, test_tenant, campaign_and_prospects, monkeypatch
):
    """The property that matters. An outage must not read as 'safe to send'."""
    campaign_id, prospect_ids = campaign_and_prospects
    monkeypatch.setattr(
        mcp_tools, "list_servers", _fake_list_servers([_server_with_dnc()])
    )

    async def unavailable(server, domain):
        return mcp_tools.DncVerdict(
            available=False, blocked=False, error="transport error: refused"
        )

    monkeypatch.setattr(mcp_tools, "check_do_not_contact", unavailable)

    async with test_session_factory() as session:
        added, skipped = await _enroll(session, test_tenant, campaign_id, prospect_ids)

    assert added == 0
    assert _reasons(skipped) == ["dnc_check_unavailable"] * 2


@pytest.mark.asyncio
async def test_a_clear_verdict_allows_enrollment(
    test_session_factory, test_tenant, campaign_and_prospects, monkeypatch
):
    campaign_id, prospect_ids = campaign_and_prospects
    monkeypatch.setattr(
        mcp_tools, "list_servers", _fake_list_servers([_server_with_dnc()])
    )

    async def clear(server, domain):
        return mcp_tools.DncVerdict(available=True, blocked=False)

    monkeypatch.setattr(mcp_tools, "check_do_not_contact", clear)

    async with test_session_factory() as session:
        added, skipped = await _enroll(session, test_tenant, campaign_id, prospect_ids)

    assert added == 2
    assert skipped == []


@pytest.mark.asyncio
async def test_one_round_trip_per_domain_not_per_prospect(
    test_session_factory, test_tenant, campaign_and_prospects, monkeypatch
):
    """Both fixtures share acmegym.com; a per-prospect call would make
    enrollment O(N) network calls against someone else's rate limit."""
    campaign_id, prospect_ids = campaign_and_prospects
    monkeypatch.setattr(
        mcp_tools, "list_servers", _fake_list_servers([_server_with_dnc()])
    )
    calls = []

    async def counting(server, domain):
        calls.append(domain)
        return mcp_tools.DncVerdict(available=True, blocked=False)

    monkeypatch.setattr(mcp_tools, "check_do_not_contact", counting)

    async with test_session_factory() as session:
        added, _ = await _enroll(session, test_tenant, campaign_id, prospect_ids)

    assert added == 2
    assert calls == ["acmegym.com"]


@pytest.mark.asyncio
async def test_a_server_without_the_dnc_tool_is_not_consulted(
    test_session_factory, test_tenant, campaign_and_prospects, monkeypatch
):
    other = mcp_tools.McpServer(
        integration_id=uuid.uuid4(),
        name="other",
        endpoint="https://x/api",
        secret="k",
        tools=[{"name": "get_leads", "description": "", "input_schema": {}}],
    )
    monkeypatch.setattr(mcp_tools, "list_servers", _fake_list_servers([other]))

    async def explode(server, domain):
        raise AssertionError("must not be called")

    monkeypatch.setattr(mcp_tools, "check_do_not_contact", explode)

    campaign_id, prospect_ids = campaign_and_prospects
    async with test_session_factory() as session:
        added, skipped = await _enroll(session, test_tenant, campaign_id, prospect_ids)

    assert added == 2


def _fake_list_servers(servers):
    async def _fake(db, tenant_id):
        return list(servers)

    return _fake
