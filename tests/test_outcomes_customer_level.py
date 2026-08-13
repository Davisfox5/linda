"""Customer-level outcome attribution on POST /outcomes.

A self-serve conversion has no originating call or email, so the caller
attributes it with ``customer_id`` alone.  Before this, ``interaction_id``
was a required field and every such push 422'd in Pydantic before any
handler ran — the event never landed, and the loop that is supposed to
learn from conversions learned from nothing.

What matters here is the seam between "durable" and "believed":
customer-level events must be recorded and deduped exactly like
interaction-level ones, but must NOT reach
``InteractionFeatures.proxy_outcomes``, because crediting a self-serve
signup to whichever interactions happen to exist invents a causal link
nobody verified.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select


PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def test_customer(test_session_factory, test_tenant):
    """A customer with no interactions at all — the self-serve shape."""
    from backend.app.models import Customer

    async with test_session_factory() as session:
        customer = Customer(tenant_id=test_tenant.id, name="Flex Signup Co")
        session.add(customer)
        await session.commit()
        await session.refresh(customer)
        return customer


# ── Schema-level: what the endpoint will and won't accept ────────────────


def test_customer_id_alone_is_a_valid_event():
    from backend.app.api.outcomes import OutcomeEvent

    ev = OutcomeEvent(customer_id=uuid.uuid4(), outcome_type="deal_won")
    assert ev.interaction_id is None
    assert ev.customer_id is not None


def test_interaction_id_alone_is_still_valid():
    """The pre-existing shape must keep working untouched."""
    from backend.app.api.outcomes import OutcomeEvent

    ev = OutcomeEvent(interaction_id=uuid.uuid4(), outcome_type="deal_won")
    assert ev.customer_id is None


def test_an_event_with_neither_id_is_rejected():
    """Attributing to nothing is not a degraded event, it's a useless
    one — it must fail loudly at the door rather than land unjoinable."""
    from pydantic import ValidationError

    from backend.app.api.outcomes import OutcomeEvent

    with pytest.raises(ValidationError) as exc:
        OutcomeEvent(outcome_type="deal_won")
    assert "interaction_id or customer_id" in str(exc.value)


def test_customer_level_fingerprint_cannot_collide_with_interaction_level():
    from backend.app.api.outcomes import OutcomeEvent, _autogen_event_id

    shared = uuid.uuid4()
    by_interaction = _autogen_event_id(
        OutcomeEvent(interaction_id=shared, outcome_type="deal_won")
    )
    by_customer = _autogen_event_id(
        OutcomeEvent(customer_id=shared, outcome_type="deal_won")
    )
    assert by_interaction != by_customer


def test_interaction_level_fingerprint_format_is_frozen():
    """Reminting these would let an in-flight retry of an event_id-less
    payload land a second time, which is exactly what the fingerprint
    exists to prevent."""
    import hashlib

    from backend.app.api.outcomes import OutcomeEvent, _autogen_event_id

    iid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    expected_key = f"{iid}|deal_won|"
    expected = "auto:" + hashlib.sha256(expected_key.encode()).hexdigest()[:32]
    assert (
        _autogen_event_id(OutcomeEvent(interaction_id=iid, outcome_type="deal_won"))
        == expected
    )


# ── End to end ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_customer_only_event_is_accepted_and_recorded(
    test_client, test_customer, test_session
):
    from backend.app.models import OutcomeEventIngestion

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "customer_id": str(test_customer.id),
            "outcome_type": "deal_won",
            "event_id": "flex-conv-001",
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": 1, "duplicate": 0, "dropped": 0}

    row = (
        await test_session.execute(
            select(OutcomeEventIngestion).where(
                OutcomeEventIngestion.event_id == "flex-conv-001"
            )
        )
    ).scalar_one()
    assert row.customer_id == test_customer.id
    assert row.interaction_id is None
    assert row.outcome_type == "deal_won"


@pytest.mark.asyncio
async def test_customer_only_event_does_not_touch_proxy_outcomes(
    test_client, test_customer, test_interaction, test_session
):
    """The whole point of keeping this separate: a customer-level
    conversion must not become interaction-level evidence for a call it
    may have had nothing to do with."""
    from backend.app.models import InteractionFeatures

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "customer_id": str(test_customer.id),
            "outcome_type": "deal_won",
            "event_id": "flex-conv-002",
        },
    )
    assert resp.status_code == 202

    features = (
        await test_session.execute(
            select(InteractionFeatures).where(
                InteractionFeatures.interaction_id == test_interaction.id
            )
        )
    ).scalar_one()
    assert not (features.proxy_outcomes or {})


@pytest.mark.asyncio
async def test_retry_of_a_customer_event_is_a_duplicate_not_a_second_accept(
    test_client, test_customer
):
    """A daily reconcile cron re-pushing the same conversion must never
    double-count it."""
    body = {
        "customer_id": str(test_customer.id),
        "outcome_type": "deal_won",
        "event_id": "flex-conv-003",
    }
    first = await test_client.post(f"{PREFIX}/outcomes", json=body)
    assert first.json()["accepted"] == 1

    second = await test_client.post(f"{PREFIX}/outcomes", json=body)
    assert second.status_code == 202
    assert second.json() == {"accepted": 0, "duplicate": 1, "dropped": 0}


@pytest.mark.asyncio
async def test_customer_event_without_event_id_dedupes_on_fingerprint(
    test_client, test_customer
):
    body = {
        "customer_id": str(test_customer.id),
        "outcome_type": "deal_won",
        "occurred_at": "2026-08-01T00:00:00+00:00",
    }
    assert (await test_client.post(f"{PREFIX}/outcomes", json=body)).json()[
        "accepted"
    ] == 1
    assert (await test_client.post(f"{PREFIX}/outcomes", json=body)).json()[
        "duplicate"
    ] == 1


@pytest.mark.asyncio
async def test_unknown_customer_is_rejected_and_dead_lettered(
    test_client, test_session
):
    """An orphan ingestion row keyed to a typo'd id is worse than a
    dead-letter: nothing ever joins to it, so the miss is invisible."""
    from backend.app.models import DroppedOutcomeEvent

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "customer_id": str(uuid.uuid4()),
            "outcome_type": "deal_won",
            "event_id": "flex-conv-bogus",
        },
    )
    assert resp.status_code == 422
    assert "customer_not_found" in resp.text

    dropped = (
        (await test_session.execute(select(DroppedOutcomeEvent))).scalars().all()
    )
    assert any(d.reason == "customer_not_found" for d in dropped)


@pytest.mark.asyncio
async def test_cross_tenant_customer_is_not_reachable(
    test_client, test_session_factory, test_session
):
    """Tenant scoping is re-checked on the customer lookup, not inherited
    from the payload."""
    from backend.app.models import Customer, Tenant

    async with test_session_factory() as session:
        other = Tenant(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
        session.add(other)
        await session.commit()
        await session.refresh(other)
        foreign = Customer(tenant_id=other.id, name="Not Yours")
        session.add(foreign)
        await session.commit()
        await session.refresh(foreign)

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "customer_id": str(foreign.id),
            "outcome_type": "deal_won",
            "event_id": "flex-conv-crosstenant",
        },
    )
    assert resp.status_code == 422
    assert "customer_not_found" in resp.text


@pytest.mark.asyncio
async def test_batch_mixes_interaction_and_customer_level_events(
    test_client, test_customer, test_interaction, test_session
):
    from backend.app.models import InteractionFeatures

    resp = await test_client.post(
        f"{PREFIX}/outcomes/batch",
        json={
            "events": [
                {
                    "interaction_id": str(test_interaction.id),
                    "outcome_type": "customer_replied",
                    "event_id": "mixed-a",
                },
                {
                    "customer_id": str(test_customer.id),
                    "outcome_type": "deal_won",
                    "event_id": "mixed-b",
                },
            ]
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 2

    # Only the interaction-level one reached the calibrator's input.
    features = (
        await test_session.execute(
            select(InteractionFeatures).where(
                InteractionFeatures.interaction_id == test_interaction.id
            )
        )
    ).scalar_one()
    assert "customer_replied" in (features.proxy_outcomes or {})
    assert "deal_won" not in (features.proxy_outcomes or {})


@pytest.mark.asyncio
async def test_batch_customer_miss_is_dropped_not_fatal(
    test_client, test_customer
):
    """Batch semantics are unchanged: one bad event is counted, the rest
    still land."""
    resp = await test_client.post(
        f"{PREFIX}/outcomes/batch",
        json={
            "events": [
                {
                    "customer_id": str(uuid.uuid4()),
                    "outcome_type": "deal_won",
                    "event_id": "batch-bogus",
                },
                {
                    "customer_id": str(test_customer.id),
                    "outcome_type": "deal_won",
                    "event_id": "batch-good",
                },
            ]
        },
    )
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 1, "duplicate": 0, "dropped": 1}


@pytest.mark.asyncio
async def test_event_with_neither_id_422s_at_the_endpoint(test_client):
    resp = await test_client.post(
        f"{PREFIX}/outcomes", json={"outcome_type": "deal_won"}
    )
    assert resp.status_code == 422
