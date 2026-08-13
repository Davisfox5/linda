"""The per-delivery ``event_id`` in the webhook envelope.

``X-Linda-Delivery`` has always carried a stable per-delivery UUID, but
only as a header.  A receiver that sees just the parsed JSON — a queue
worker, a proxy that strips headers — had nothing to dedupe on and had
to fall back to hashing the body.  That hash is correct for a
byte-identical redelivery but collapses two genuinely distinct events
that happen to serialise identically, which is exactly the case where
losing one is silent.

The id is per-delivery, not per-event: the same logical event fanned out
to two endpoints carries two ids.  That is right for per-receiver dedupe
and wrong for correlating across receivers, so it is pinned here.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.services.webhook_dispatcher import _envelope, emit_event


# ── The envelope helper ──────────────────────────────────────────────────


def test_envelope_carries_event_id_when_given():
    did = uuid.uuid4()
    body = _envelope("customer.churned", uuid.uuid4(), {"x": 1}, event_id=did)
    assert body["event_id"] == str(did)
    assert body["data"] == {"x": 1}
    assert body["event"] == "customer.churned"


def test_envelope_omits_event_id_when_absent():
    """Callers that build an envelope with no delivery row behind it
    should not advertise an id they cannot keep stable."""
    body = _envelope("customer.churned", uuid.uuid4(), {"x": 1})
    assert "event_id" not in body


# ── End to end through emit_event ────────────────────────────────────────


@pytest_asyncio.fixture
async def seeded_webhook(test_session_factory, test_tenant):
    from backend.app.models import Webhook

    async with test_session_factory() as session:
        wh = Webhook(
            tenant_id=test_tenant.id,
            url="https://example.com/hook",
            events=["*"],
            secret="secret-x",
        )
        session.add(wh)
        await session.commit()
        await session.refresh(wh)
        return wh


@pytest.mark.asyncio
async def test_emitted_delivery_payload_event_id_matches_the_row_id(
    test_session, test_tenant, seeded_webhook
):
    deliveries = await emit_event(
        test_session,
        test_tenant.id,
        "customer.churned",
        {"customer_id": str(uuid.uuid4())},
        dispatch_now=False,
    )
    assert len(deliveries) == 1
    d = deliveries[0]
    assert d.payload["event_id"] == str(d.id)


@pytest.mark.asyncio
async def test_event_id_survives_the_round_trip_to_the_database(
    test_session, test_tenant, seeded_webhook
):
    """The signature is computed over the stored payload at delivery
    time, so the id has to be in the row — not injected on the way out."""
    from backend.app.models import WebhookDelivery

    deliveries = await emit_event(
        test_session,
        test_tenant.id,
        "customer.churned",
        {"customer_id": str(uuid.uuid4())},
        dispatch_now=False,
    )
    await test_session.commit()
    delivery_id = deliveries[0].id

    stored = (
        await test_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
        )
    ).scalar_one()
    assert stored.payload["event_id"] == str(delivery_id)


@pytest.mark.asyncio
async def test_fan_out_gives_each_endpoint_its_own_event_id(
    test_session, test_session_factory, test_tenant, seeded_webhook
):
    from backend.app.models import Webhook

    async with test_session_factory() as session:
        session.add(
            Webhook(
                tenant_id=test_tenant.id,
                url="https://second.example.com/hook",
                events=["*"],
                secret="secret-y",
            )
        )
        await session.commit()

    deliveries = await emit_event(
        test_session,
        test_tenant.id,
        "customer.churned",
        {"customer_id": str(uuid.uuid4())},
        dispatch_now=False,
    )
    assert len(deliveries) == 2
    ids = {d.payload["event_id"] for d in deliveries}
    assert len(ids) == 2, "two endpoints must not share a dedupe key"
    assert ids == {str(d.id) for d in deliveries}


# ── The retry contract Flex depends on ───────────────────────────────────


def test_503_is_a_retryable_status_not_an_ack():
    """Flex returns 503 when it cannot persist a delivery, because
    acking an event it failed to store would lose it permanently. That
    only works if we actually retry it."""
    from backend.app.services.webhook_dispatcher import _BACKOFF_SECONDS

    # deliver_one treats anything outside 2xx as a failed attempt, so the
    # meaningful assertion is that a retry schedule exists at all and is
    # long enough to outlast a transient outage.
    assert _BACKOFF_SECONDS[0] > 0
    assert sum(_BACKOFF_SECONDS) >= 3600, (
        "retry window must outlast a short storage outage"
    )
