"""Tests for Ask LINDA's action-step dispatch — the point where chat stops
staging drafts and starts having real-world effect.

The properties worth pinning down are the refusals: a Confirm button must
never appear for a step that can't be sent, the check must be re-run at
confirm time (proposals live 24h), and the send must go through the same
dispatch path the manual endpoints and the auto-executor use.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import linda_dispatch


def _ctx(session, tenant, user=None):
    from backend.app.services.linda_agent import AgentContext

    return AgentContext(
        db=session, tenant=tenant, user=user, conversation_id=uuid.uuid4()
    )


async def _seed_step(
    session,
    tenant,
    *,
    channel="email",
    state="ready",
    plan_status="active",
    artifact_payload=None,
    with_artifact=True,
):
    from backend.app.models import ActionPlan, ActionStep, StepArtifact

    plan = ActionPlan(
        tenant_id=tenant.id, goal="Close the Acme renewal", domain="sales",
        status=plan_status, procedures_applied=[], external_context_snapshot={},
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)

    step = ActionStep(
        plan_id=plan.id, tenant_id=tenant.id, title="Send the renewal quote",
        intent="Get the quote in front of Dana", state=state,
        recommended_channel=channel, participants=[], prep_artifacts=[],
        depends_on=[], input_slots=[], output_schema=[], output_data={},
        role_in_plan="customer_endpoint", artifact_version=1,
    )
    session.add(step)
    await session.commit()
    await session.refresh(step)

    if with_artifact:
        payload = artifact_payload if artifact_payload is not None else {
            "subject": "Your 2026 renewal quote",
            "body": "Hi Dana — attached is the quote we discussed.",
            "unfilled_slots": [],
        }
        session.add(
            StepArtifact(
                step_id=step.id, tenant_id=tenant.id, version=1,
                kind="email", payload=payload,
            )
        )
        await session.commit()
    return plan, step


# ── preview / pre-flight refusals ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_returns_the_real_content_that_will_be_sent(
    test_session, test_tenant
):
    """The user confirms the rendered artifact, not Linda's paraphrase."""
    plan, step = await _seed_step(test_session, test_tenant)

    out = await linda_dispatch.preview(test_session, test_tenant, str(step.id))

    assert out["step_id"] == str(step.id)
    assert out["channel"] == "email"
    assert "sends this email" in out["on_confirm"]
    assert out["content"]["subject"] == "Your 2026 renewal quote"


@pytest.mark.asyncio
async def test_unfilled_placeholders_refuse_before_a_confirm_button_exists(
    test_session, test_tenant
):
    """An artifact with unfilled slots still contains literal {{tokens}} —
    sending that to a customer is visible and unrecoverable."""
    plan, step = await _seed_step(
        test_session, test_tenant,
        artifact_payload={
            "subject": "Quote for {{company}}",
            "body": "Hi {{first_name}}",
            "unfilled_slots": ["company", "first_name"],
        },
    )

    out = await linda_dispatch.preview(test_session, test_tenant, str(step.id))

    assert "unfilled placeholders" in out["error"]
    assert "company" in out["error"]


@pytest.mark.asyncio
async def test_already_done_step_is_refused(test_session, test_tenant):
    plan, step = await _seed_step(test_session, test_tenant, state="done")
    out = await linda_dispatch.preview(test_session, test_tenant, str(step.id))
    assert "duplicate" in out["error"]


@pytest.mark.asyncio
async def test_awaiting_response_step_is_refused(test_session, test_tenant):
    plan, step = await _seed_step(test_session, test_tenant, state="awaiting_response")
    out = await linda_dispatch.preview(test_session, test_tenant, str(step.id))
    assert "nothing to send" in out["error"]


@pytest.mark.asyncio
async def test_step_without_generated_content_is_refused(test_session, test_tenant):
    plan, step = await _seed_step(test_session, test_tenant, with_artifact=False)
    out = await linda_dispatch.preview(test_session, test_tenant, str(step.id))
    assert "no generated content" in out["error"]


@pytest.mark.asyncio
async def test_non_dispatchable_channel_is_refused(test_session, test_tenant):
    """'research' and 'document_send' produce something a person delivers."""
    for channel in ("research", "document_send"):
        plan, step = await _seed_step(test_session, test_tenant, channel=channel)
        out = await linda_dispatch.preview(test_session, test_tenant, str(step.id))
        assert "no automatic send" in out["error"], channel


@pytest.mark.asyncio
async def test_inactive_plan_is_refused(test_session, test_tenant):
    plan, step = await _seed_step(test_session, test_tenant, plan_status="completed")
    out = await linda_dispatch.preview(test_session, test_tenant, str(step.id))
    assert "not active" in out["error"]


@pytest.mark.asyncio
async def test_preview_is_tenant_scoped(test_session_factory, test_tenant):
    from backend.app.models import Tenant

    async with test_session_factory() as session:
        other = Tenant(name="Other", slug="o-%s" % uuid.uuid4().hex[:6])
        session.add(other)
        await session.commit()
        await session.refresh(other)
        _, foreign_step = await _seed_step(session, other)
        foreign_id = foreign_step.id

    async with test_session_factory() as session:
        out = await linda_dispatch.preview(session, test_tenant, str(foreign_id))

    assert out["error"] == "step not found"


@pytest.mark.asyncio
async def test_bad_step_id_does_not_raise(test_session, test_tenant):
    out = await linda_dispatch.preview(test_session, test_tenant, "not-a-uuid")
    assert out["error"] == "invalid step_id"


# ── proposal staging ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_tool_stages_a_proposal_carrying_the_preview(
    test_session, test_tenant
):
    from backend.app.models import WriteProposal
    from backend.app.services.linda_agent import dispatch_tool
    from sqlalchemy import select

    plan, step = await _seed_step(test_session, test_tenant)
    ctx = _ctx(test_session, test_tenant)

    result = await dispatch_tool(ctx, "propose_step_dispatch", {"step_id": str(step.id)})

    assert result["kind"] == "step_dispatch"
    assert result["preview"]["step_id"] == str(step.id)
    assert result["preview"]["content"]["subject"] == "Your 2026 renewal quote"

    rows = (await test_session.execute(select(WriteProposal))).scalars().all()
    assert [r.kind for r in rows] == ["step_dispatch"]


@pytest.mark.asyncio
async def test_unsendable_step_never_becomes_a_proposal(test_session, test_tenant):
    """The Tier 0 failure this whole effort started from: a Confirm button
    for something the confirm endpoint will reject."""
    from backend.app.models import WriteProposal
    from backend.app.services.linda_agent import dispatch_tool
    from sqlalchemy import select

    plan, step = await _seed_step(test_session, test_tenant, state="done")
    ctx = _ctx(test_session, test_tenant)

    result = await dispatch_tool(ctx, "propose_step_dispatch", {"step_id": str(step.id)})

    assert "error" in result
    assert "proposal_id" not in result
    rows = (await test_session.execute(select(WriteProposal))).scalars().all()
    assert rows == []


# ── execution ──────────────────────────────────────────────────────────────


def _dispatch_ok(**over):
    result = SimpleNamespace(
        success=True, provider="gmail", new_state="awaiting_response",
        provider_message_id="msg-123", email_send_id=uuid.uuid4(),
        external_id=None, event_id=None, error=None,
    )
    for k, v in over.items():
        setattr(result, k, v)
    return result


@pytest.mark.asyncio
async def test_execute_routes_through_the_shared_dispatch_path(
    test_session, test_tenant
):
    """Chat is a third CALLER of action_plan/dispatch.py, not a third
    implementation — the manual endpoints and auto-executor use the same
    channel router."""
    plan, step = await _seed_step(test_session, test_tenant)

    with patch(
        "backend.app.services.action_plan.executor._dispatch_for_channel",
        new=AsyncMock(return_value=_dispatch_ok()),
    ) as dispatcher:
        outcome = await linda_dispatch.execute(
            test_session, test_tenant, {"step_id": str(step.id)}
        )

    assert outcome["channel"] == "email"
    assert outcome["provider"] == "gmail"
    assert outcome["new_state"] == "awaiting_response"
    assert outcome["provider_message_id"] == "msg-123"
    kwargs = dispatcher.await_args.kwargs
    assert kwargs["channel"] == "email"
    assert kwargs["step"].id == step.id


@pytest.mark.asyncio
async def test_execute_reruns_preflight_because_proposals_live_24h(
    test_session, test_tenant
):
    """A step can be sent, skipped or regenerated between propose and
    confirm — the confirm must not trust the preview's verdict."""
    from fastapi import HTTPException

    plan, step = await _seed_step(test_session, test_tenant)
    # Staged while ready; sent by someone else in the meantime.
    step.state = "done"
    await test_session.commit()

    with patch(
        "backend.app.services.action_plan.executor._dispatch_for_channel",
        new=AsyncMock(return_value=_dispatch_ok()),
    ) as dispatcher:
        with pytest.raises(HTTPException) as exc:
            await linda_dispatch.execute(
                test_session, test_tenant, {"step_id": str(step.id)}
            )

    assert exc.value.status_code == 409
    dispatcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_surfaces_a_failed_send_rather_than_confirming(
    test_session, test_tenant
):
    from fastapi import HTTPException

    plan, step = await _seed_step(test_session, test_tenant)

    with patch(
        "backend.app.services.action_plan.executor._dispatch_for_channel",
        new=AsyncMock(return_value=SimpleNamespace(
            success=False, error="Gmail token expired", provider="gmail",
            new_state=None,
        )),
    ):
        with pytest.raises(HTTPException) as exc:
            await linda_dispatch.execute(
                test_session, test_tenant, {"step_id": str(step.id)}
            )

    assert exc.value.status_code == 502
    assert "Gmail token expired" in exc.value.detail


@pytest.mark.asyncio
async def test_execute_surfaces_a_raising_dispatch(test_session, test_tenant):
    from fastapi import HTTPException

    plan, step = await _seed_step(test_session, test_tenant)

    with patch(
        "backend.app.services.action_plan.executor._dispatch_for_channel",
        new=AsyncMock(side_effect=RuntimeError("smtp exploded")),
    ):
        with pytest.raises(HTTPException) as exc:
            await linda_dispatch.execute(
                test_session, test_tenant, {"step_id": str(step.id)}
            )

    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_confirming_a_step_dispatch_proposal_executes_it(
    test_session, test_tenant
):
    """End-to-end through the confirm endpoint: the proposal is marked
    confirmed and the dispatch outcome is recorded on it for audit."""
    from datetime import datetime, timedelta, timezone

    from backend.app.api import chat as chat_module
    from backend.app.models import WriteProposal

    plan, step = await _seed_step(test_session, test_tenant)
    proposal = WriteProposal(
        conversation_id=uuid.uuid4(), tenant_id=test_tenant.id, kind="step_dispatch",
        payload={"step_id": str(step.id), "channel": "email"}, status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    test_session.add(proposal)
    await test_session.commit()

    with patch(
        "backend.app.services.action_plan.executor._dispatch_for_channel",
        new=AsyncMock(return_value=_dispatch_ok()),
    ):
        out = await chat_module.confirm_proposal(
            proposal.id, tenant=test_tenant, db=test_session
        )

    assert out.status == "confirmed"
    assert out.payload["dispatch_result"]["provider"] == "gmail"
    assert out.payload["dispatch_result"]["new_state"] == "awaiting_response"


# ── the safety contract ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_is_not_gated_on_the_unattended_execution_flag(
    test_session, test_tenant
):
    """AUTO_EXECUTION_ENABLED governs UNATTENDED dispatch — the executor
    acting with nobody in the loop. A user clicking Confirm is the human
    approval that flag exists to require, exactly like the manual
    /send-email endpoint, which is likewise ungated."""
    from backend.app.config import get_settings

    assert get_settings().AUTO_EXECUTION_ENABLED is False

    plan, step = await _seed_step(test_session, test_tenant)
    with patch(
        "backend.app.services.action_plan.executor._dispatch_for_channel",
        new=AsyncMock(return_value=_dispatch_ok()),
    ):
        outcome = await linda_dispatch.execute(
            test_session, test_tenant, {"step_id": str(step.id)}
        )

    assert outcome["new_state"] == "awaiting_response"


def test_dispatchable_channels_match_the_auto_executors():
    """Two lists of "what can be sent" would eventually disagree."""
    from backend.app.services.action_plan.executor import _DISPATCHABLE_CHANNELS

    assert linda_dispatch.DISPATCHABLE_CHANNELS == _DISPATCHABLE_CHANNELS


def test_step_dispatch_proposals_are_preflighted():
    from backend.app.services.linda_agent import (
        DRAFT_KIND_BY_TOOL,
        PREFLIGHT_DRAFT_TOOLS,
    )

    assert "propose_step_dispatch" in PREFLIGHT_DRAFT_TOOLS
    assert DRAFT_KIND_BY_TOOL["propose_step_dispatch"] == "step_dispatch"
