"""Tests for the Ask LINDA outcome flywheel (gap G5).

What matters here is that the loop is *grounded*: every verdict traces to
a real row changing, a cancel is captured as evidence, and the absence of
a signal is never silently counted as failure.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app.services import linda_outcomes


def _proposal(kind="action_item", tenant_id=None, **over):
    payload = dict(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        kind=kind,
        payload={},
    )
    payload.update(over)
    return SimpleNamespace(**payload)


# ── Recording ──────────────────────────────────────────────────────────────


def test_cancel_is_captured_as_a_rejection():
    """The strongest negative signal chat produces, and it used to be
    thrown away entirely."""
    row = linda_outcomes.build_outcome(_proposal(), "cancelled")

    assert row.decision == "cancelled"
    assert row.outcome == "rejected"
    assert row.observed_at is not None  # settled immediately, nothing to wait for


def test_expiry_is_not_counted_against_the_proposal():
    """An expired proposal means the user never came back — that says
    nothing about whether the suggestion was good."""
    row = linda_outcomes.build_outcome(_proposal(), "expired")

    assert row.decision == "expired"
    assert row.outcome == "no_signal"


def test_confirmed_proposals_start_pending():
    row = linda_outcomes.build_outcome(_proposal(), "confirmed", uuid.uuid4())

    assert row.outcome == "pending"
    assert row.observed_at is None


def test_crm_update_is_resolved_at_confirm_time():
    """The confirm path already executed it and would have raised — there
    is nothing downstream to wait for."""
    row = linda_outcomes.build_outcome(_proposal(kind="crm_update"), "confirmed")

    assert row.outcome == "succeeded"
    assert "executed at confirm" in row.outcome_detail["reason"]


def test_extra_detail_is_carried_for_observers_that_need_it():
    """step_dispatch lands on an existing step, so resulting_entity_id
    can't identify it — the step id has to be recorded up front."""
    step_id = str(uuid.uuid4())
    row = linda_outcomes.build_outcome(
        _proposal(kind="step_dispatch"), "confirmed", None,
        extra_detail={"step_id": step_id},
    )

    assert row.outcome_detail["step_id"] == step_id


# ── Observation (sync session) ─────────────────────────────────────────────


@pytest.fixture()
def sync_session():
    """A sync SQLite session with every table created — the observer runs
    on the Celery/sync side, not the async request path."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_outcome(session, tenant_id, kind, resulting_entity_id=None,
                  decided_at=None, detail=None):
    from backend.app.models import LindaActionOutcome

    row = LindaActionOutcome(
        tenant_id=tenant_id,
        proposal_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        kind=kind,
        decision="confirmed",
        decided_at=decided_at or datetime.now(timezone.utc),
        resulting_entity_id=resulting_entity_id,
        outcome="pending",
        outcome_detail=detail or {},
        observation_attempts=0,
    )
    session.add(row)
    session.commit()
    return row


def _tenant(session):
    from backend.app.models import Tenant

    tenant = Tenant(name="T", slug="t-%s" % uuid.uuid4().hex[:8])
    session.add(tenant)
    session.commit()
    return tenant


def test_completed_action_item_resolves_as_succeeded(sync_session):
    from backend.app.models import ActionItem, Interaction

    tenant = _tenant(sync_session)
    interaction = Interaction(tenant_id=tenant.id, channel="voice")
    sync_session.add(interaction)
    sync_session.commit()

    item = ActionItem(
        tenant_id=tenant.id, interaction_id=interaction.id,
        title="Send the quote", status="done",
    )
    sync_session.add(item)
    sync_session.commit()

    row = _seed_outcome(sync_session, tenant.id, "action_item", item.id)
    counts = linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "succeeded"
    assert row.outcome_detail["action_item_status"] == "done"
    assert counts["resolved"] == 1


def test_dismissed_action_item_resolves_as_rejected(sync_session):
    """Taking the item and then throwing it away is a real negative — the
    proposal was accepted but turned out not to be wanted."""
    from backend.app.models import ActionItem, Interaction

    tenant = _tenant(sync_session)
    interaction = Interaction(tenant_id=tenant.id, channel="voice")
    sync_session.add(interaction)
    sync_session.commit()
    item = ActionItem(
        tenant_id=tenant.id, interaction_id=interaction.id,
        title="Nope", status="dismissed",
    )
    sync_session.add(item)
    sync_session.commit()

    row = _seed_outcome(sync_session, tenant.id, "action_item", item.id)
    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "rejected"


def test_open_action_item_stays_pending_inside_the_horizon(sync_session):
    from backend.app.models import ActionItem, Interaction

    tenant = _tenant(sync_session)
    interaction = Interaction(tenant_id=tenant.id, channel="voice")
    sync_session.add(interaction)
    sync_session.commit()
    item = ActionItem(
        tenant_id=tenant.id, interaction_id=interaction.id,
        title="Still open", status="open",
    )
    sync_session.add(item)
    sync_session.commit()

    row = _seed_outcome(sync_session, tenant.id, "action_item", item.id)
    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "pending"
    assert row.observation_attempts == 1


def test_unresolved_past_the_horizon_becomes_no_signal_not_failure(sync_session):
    """Absence of evidence is not evidence of failure — an item nobody has
    touched in three weeks must not drag a proposal kind's success rate
    down."""
    from backend.app.models import ActionItem, Interaction

    tenant = _tenant(sync_session)
    interaction = Interaction(tenant_id=tenant.id, channel="voice")
    sync_session.add(interaction)
    sync_session.commit()
    item = ActionItem(
        tenant_id=tenant.id, interaction_id=interaction.id,
        title="Forgotten", status="open",
    )
    sync_session.add(item)
    sync_session.commit()

    stale = datetime.now(timezone.utc) - linda_outcomes.OBSERVATION_HORIZON - timedelta(days=1)
    row = _seed_outcome(sync_session, tenant.id, "action_item", item.id, decided_at=stale)
    counts = linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "no_signal"
    assert counts["expired"] == 1


def test_replied_outreach_member_resolves_as_succeeded(sync_session):
    from backend.app.models import Campaign, Customer, OutreachMember

    tenant = _tenant(sync_session)
    campaign = Campaign(tenant_id=tenant.id, name="Q3", channel="email", kind="outreach")
    customer = Customer(tenant_id=tenant.id, name="Acme")
    sync_session.add_all([campaign, customer])
    sync_session.commit()
    member = OutreachMember(
        tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id,
        state="replied",
    )
    sync_session.add(member)
    sync_session.commit()

    row = _seed_outcome(sync_session, tenant.id, "queue_bump_email", member.id)
    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "succeeded"


def test_bounced_outreach_member_resolves_as_failed(sync_session):
    """Carried out and demonstrably didn't land — distinct from rejected
    (the human declined) and no_signal (nothing happened)."""
    from backend.app.models import Campaign, Customer, OutreachMember

    tenant = _tenant(sync_session)
    campaign = Campaign(tenant_id=tenant.id, name="Q3", channel="email", kind="outreach")
    customer = Customer(tenant_id=tenant.id, name="Acme")
    sync_session.add_all([campaign, customer])
    sync_session.commit()
    member = OutreachMember(
        tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id,
        state="bounced",
    )
    sync_session.add(member)
    sync_session.commit()

    row = _seed_outcome(sync_session, tenant.id, "queue_bump_email", member.id)
    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "failed"


def test_step_dispatch_resolves_from_the_step_recorded_in_detail(sync_session):
    from backend.app.models import ActionPlan, ActionStep

    tenant = _tenant(sync_session)
    plan = ActionPlan(
        tenant_id=tenant.id, goal="g", domain="sales", status="active",
        procedures_applied=[], external_context_snapshot={},
    )
    sync_session.add(plan)
    sync_session.commit()
    step = ActionStep(
        plan_id=plan.id, tenant_id=tenant.id, title="Send it", state="done",
        recommended_channel="email", participants=[], prep_artifacts=[],
        depends_on=[], input_slots=[], output_schema=[], output_data={},
        role_in_plan="customer_endpoint",
    )
    sync_session.add(step)
    sync_session.commit()

    row = _seed_outcome(
        sync_session, tenant.id, "step_dispatch", detail={"step_id": str(step.id)}
    )
    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "succeeded"


def test_awaiting_response_step_stays_pending(sync_session):
    """The send worked; the reply hasn't come. Undecided, not failed."""
    from backend.app.models import ActionPlan, ActionStep

    tenant = _tenant(sync_session)
    plan = ActionPlan(
        tenant_id=tenant.id, goal="g", domain="sales", status="active",
        procedures_applied=[], external_context_snapshot={},
    )
    sync_session.add(plan)
    sync_session.commit()
    step = ActionStep(
        plan_id=plan.id, tenant_id=tenant.id, title="Sent", state="awaiting_response",
        recommended_channel="email", participants=[], prep_artifacts=[],
        depends_on=[], input_slots=[], output_schema=[], output_data={},
        role_in_plan="customer_endpoint",
    )
    sync_session.add(step)
    sync_session.commit()

    row = _seed_outcome(
        sync_session, tenant.id, "step_dispatch", detail={"step_id": str(step.id)}
    )
    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "pending"


def test_observation_is_tenant_scoped(sync_session):
    from backend.app.models import ActionItem, Interaction

    mine = _tenant(sync_session)
    theirs = _tenant(sync_session)
    interaction = Interaction(tenant_id=theirs.id, channel="voice")
    sync_session.add(interaction)
    sync_session.commit()
    item = ActionItem(
        tenant_id=theirs.id, interaction_id=interaction.id,
        title="Theirs", status="done",
    )
    sync_session.add(item)
    sync_session.commit()

    # An outcome on MY tenant pointing at THEIR action item must not
    # resolve from it.
    row = _seed_outcome(sync_session, mine.id, "action_item", item.id)
    linda_outcomes.observe_tenant(sync_session, mine.id)

    assert row.outcome == "no_signal"
    assert row.outcome_detail["reason"] == "action item deleted"


def test_a_failing_observer_does_not_stop_the_sweep(sync_session, monkeypatch):
    from backend.app.models import ActionItem, Interaction

    tenant = _tenant(sync_session)
    interaction = Interaction(tenant_id=tenant.id, channel="voice")
    sync_session.add(interaction)
    sync_session.commit()
    good = ActionItem(
        tenant_id=tenant.id, interaction_id=interaction.id, title="ok", status="done",
    )
    sync_session.add(good)
    sync_session.commit()

    bad_row = _seed_outcome(
        sync_session, tenant.id, "queue_bump_email", uuid.uuid4(),
        decided_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    good_row = _seed_outcome(sync_session, tenant.id, "action_item", good.id)

    def _boom(session, row):
        raise RuntimeError("observer blew up")

    monkeypatch.setitem(linda_outcomes._OBSERVERS, "queue_bump_email", _boom)
    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert bad_row.outcome == "pending"      # untouched
    assert good_row.outcome == "succeeded"   # sweep continued


def test_unknown_kind_ages_out_rather_than_sticking_forever(sync_session):
    tenant = _tenant(sync_session)
    stale = datetime.now(timezone.utc) - linda_outcomes.OBSERVATION_HORIZON - timedelta(days=1)
    row = _seed_outcome(sync_session, tenant.id, "some_future_kind", decided_at=stale)

    linda_outcomes.observe_tenant(sync_session, tenant.id)

    assert row.outcome == "no_signal"


# ── Aggregate ──────────────────────────────────────────────────────────────


def test_acceptance_summary_rates_ignore_unresolved_rows(sync_session):
    """pending/no_signal must stay out of the success denominator, or every
    rate looks worse the more recent the data is."""
    from backend.app.models import LindaActionOutcome

    tenant = _tenant(sync_session)
    for decision, outcome in [
        ("confirmed", "succeeded"),
        ("confirmed", "succeeded"),
        ("confirmed", "failed"),
        ("confirmed", "pending"),
        ("confirmed", "no_signal"),
        ("cancelled", "rejected"),
    ]:
        sync_session.add(
            LindaActionOutcome(
                tenant_id=tenant.id, proposal_id=uuid.uuid4(), kind="email_draft",
                decision=decision, decided_at=datetime.now(timezone.utc),
                outcome=outcome, outcome_detail={},
            )
        )
    sync_session.commit()

    summary = linda_outcomes.acceptance_summary(sync_session, tenant.id)
    entry = summary["by_kind"]["email_draft"]

    assert entry["proposed"] == 6
    assert entry["confirmed"] == 5
    assert entry["cancelled"] == 1
    assert entry["confirm_rate"] == round(5 / 6, 3)
    # Resolved = 2 succeeded + 1 failed + 1 rejected; pending/no_signal excluded.
    assert entry["success_rate"] == round(2 / 4, 3)


def test_acceptance_summary_is_empty_for_a_tenant_with_no_history(sync_session):
    tenant = _tenant(sync_session)
    assert linda_outcomes.acceptance_summary(sync_session, tenant.id)["by_kind"] == {}


# ── Wiring: the request path actually records ──────────────────────────────


@pytest.mark.asyncio
async def test_confirming_a_proposal_records_a_pending_outcome(
    test_session, test_tenant
):
    from sqlalchemy import select

    from backend.app.api import chat as chat_module
    from backend.app.models import (
        ActionItem,
        Interaction,
        LindaActionOutcome,
        LindaChatConversation,
        WriteProposal,
    )

    interaction = Interaction(tenant_id=test_tenant.id, channel="voice")
    convo = LindaChatConversation(tenant_id=test_tenant.id)
    test_session.add_all([interaction, convo])
    await test_session.commit()
    await test_session.refresh(interaction)
    await test_session.refresh(convo)

    proposal = WriteProposal(
        conversation_id=convo.id, tenant_id=test_tenant.id, kind="action_item",
        payload={"title": "Call Acme", "interaction_id": str(interaction.id)},
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    test_session.add(proposal)
    await test_session.commit()

    await chat_module.confirm_proposal(
        proposal.id, tenant=test_tenant, db=test_session
    )

    rows = (await test_session.execute(select(LindaActionOutcome))).scalars().all()
    assert len(rows) == 1
    assert rows[0].decision == "confirmed"
    assert rows[0].outcome == "pending"
    assert rows[0].kind == "action_item"
    # resulting_entity_id points at the created row, so the observer can
    # follow it without re-deriving anything.
    item = (await test_session.execute(select(ActionItem))).scalars().one()
    assert rows[0].resulting_entity_id == item.id


@pytest.mark.asyncio
async def test_cancelling_a_proposal_records_the_rejection(test_session, test_tenant):
    from sqlalchemy import select

    from backend.app.api import chat as chat_module
    from backend.app.models import (
        LindaActionOutcome,
        LindaChatConversation,
        WriteProposal,
    )

    convo = LindaChatConversation(tenant_id=test_tenant.id)
    test_session.add(convo)
    await test_session.commit()
    await test_session.refresh(convo)

    proposal = WriteProposal(
        conversation_id=convo.id, tenant_id=test_tenant.id, kind="email_draft",
        payload={"subject": "no thanks"}, status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    test_session.add(proposal)
    await test_session.commit()

    await chat_module.cancel_proposal(proposal.id, tenant=test_tenant, db=test_session)

    row = (await test_session.execute(select(LindaActionOutcome))).scalars().one()
    assert row.decision == "cancelled"
    assert row.outcome == "rejected"


@pytest.mark.asyncio
async def test_a_broken_outcome_write_never_fails_the_users_confirm(
    test_session, test_tenant, monkeypatch
):
    """The flywheel is analytics. Losing a row beats 500-ing a confirm."""
    from sqlalchemy import select

    from backend.app.api import chat as chat_module
    from backend.app.models import (
        ActionItem,
        Interaction,
        LindaChatConversation,
        WriteProposal,
    )
    from backend.app.services import linda_outcomes as lo

    interaction = Interaction(tenant_id=test_tenant.id, channel="voice")
    convo = LindaChatConversation(tenant_id=test_tenant.id)
    test_session.add_all([interaction, convo])
    await test_session.commit()
    await test_session.refresh(interaction)
    await test_session.refresh(convo)

    proposal = WriteProposal(
        conversation_id=convo.id, tenant_id=test_tenant.id, kind="action_item",
        payload={"title": "Still works", "interaction_id": str(interaction.id)},
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    test_session.add(proposal)
    await test_session.commit()

    def _boom(*a, **kw):
        raise RuntimeError("analytics exploded")

    monkeypatch.setattr(lo, "build_outcome", _boom)

    out = await chat_module.confirm_proposal(
        proposal.id, tenant=test_tenant, db=test_session
    )

    assert out.status == "confirmed"
    assert (await test_session.execute(select(ActionItem))).scalars().first() is not None


# ── No LLM in this loop ────────────────────────────────────────────────────


def test_the_outcome_loop_calls_no_model():
    """Grounded by construction: if this module ever imports the router or
    a model catalog, the loop has become the coherence trap it exists to
    avoid (an LLM judging whether its own suggestion was good)."""
    import inspect

    source = inspect.getsource(linda_outcomes)
    for forbidden in ("model_router", "ModelRouter", "model_catalog", "anthropic"):
        assert forbidden not in source, forbidden
