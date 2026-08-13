"""The /chat stream correlates tool calls to their answers.

Flex's console pairs a `tool_use` frame to the frame that answers it in
order to render "what the agent is doing". It used to have to pair on
arrival order, because no id was on the wire. Now every `tool_use` carries
a `tool_use_id` and is answered by exactly one `tool_result` **or** one
`proposal` bearing the same id.

This drives ``run_chat_turn`` with a faked model stream rather than reading
the source, because the property that matters is what reaches the socket.
"""

import uuid
from typing import Any, Dict, List

import pytest
import pytest_asyncio

from backend.app.services import linda_agent


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Final:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeStream:
    """Async CM matching the slice of the streaming API run_chat_turn uses."""

    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            return
            yield  # pragma: no cover — no text deltas needed here

        return _gen()

    async def get_final_message(self):
        return self._final


class _FakeRouter:
    """Replays a scripted list of final messages, one per agent loop."""

    def __init__(self, finals):
        self._finals = list(finals)

    def __call__(self, client):  # constructed as ModelRouter(client)
        return self

    def astream(self, req):
        return _FakeStream(self._finals.pop(0))


@pytest_asyncio.fixture
async def chat_ctx(test_session_factory, test_tenant):
    from backend.app.models import LindaChatConversation

    async with test_session_factory() as session:
        convo = LindaChatConversation(tenant_id=test_tenant.id, title="t")
        session.add(convo)
        await session.commit()
        await session.refresh(convo)
        convo_id = convo.id

    async with test_session_factory() as session:
        yield linda_agent.AgentContext(
            db=session, tenant=test_tenant, user=None, conversation_id=convo_id
        )


def _script(router, monkeypatch):
    monkeypatch.setattr(linda_agent, "ModelRouter", router)
    monkeypatch.setattr(linda_agent, "get_async_anthropic", lambda: object())


async def _collect(ctx, message="hi") -> List[Dict[str, Any]]:
    return [ev async for ev in linda_agent.run_chat_turn(ctx, message)]


@pytest.mark.asyncio
async def test_a_read_tool_result_carries_its_tool_use_id(chat_ctx, monkeypatch):
    calls = [
        _Final(
            [_Block("tool_use", id="toolu_ABC", name="get_team_metrics", input={"period": "30d"})],
            "tool_use",
        ),
        _Final([_Block("text", text="Done.")], "end_turn"),
    ]
    _script(_FakeRouter(calls), monkeypatch)
    monkeypatch.setattr(
        linda_agent, "dispatch_tool", _async_return({"calls": 12})
    )

    events = await _collect(chat_ctx)

    use = _one(events, "tool_use")
    result = _one(events, "tool_result")
    assert use["tool_use_id"] == "toolu_ABC"
    assert result["tool_use_id"] == "toolu_ABC"
    assert use["tool"] == result["tool"] == "get_team_metrics"


@pytest.mark.asyncio
async def test_a_draft_tool_proposal_carries_its_tool_use_id(chat_ctx, monkeypatch):
    """The frame that replaces tool_result must be pairable too — otherwise
    the most consequential action the agent takes is the one the console
    cannot match to its call."""
    calls = [
        _Final(
            [_Block("tool_use", id="toolu_XYZ", name="propose_action_item", input={"title": "x"})],
            "tool_use",
        ),
        _Final([_Block("text", text="Staged.")], "end_turn"),
    ]
    _script(_FakeRouter(calls), monkeypatch)
    monkeypatch.setattr(
        linda_agent,
        "dispatch_tool",
        _async_return({"proposal_id": str(uuid.uuid4()), "kind": "action_item"}),
    )

    events = await _collect(chat_ctx)

    use = _one(events, "tool_use")
    proposal = _one(events, "proposal")
    assert use["tool_use_id"] == "toolu_XYZ"
    assert proposal["tool_use_id"] == "toolu_XYZ"
    assert "tool_result" not in [e["type"] for e in events]


@pytest.mark.asyncio
async def test_parallel_tool_calls_pair_by_id_not_by_order(chat_ctx, monkeypatch):
    """The reason the id exists. With two calls in one assistant turn, an
    order-based parser is only right by luck."""
    calls = [
        _Final(
            [
                _Block("tool_use", id="toolu_1", name="get_team_metrics", input={}),
                _Block("tool_use", id="toolu_2", name="list_action_plans", input={}),
            ],
            "tool_use",
        ),
        _Final([_Block("text", text="ok")], "end_turn"),
    ]
    _script(_FakeRouter(calls), monkeypatch)

    async def _dispatch(ctx, name, args):
        return {"tool": name}

    monkeypatch.setattr(linda_agent, "dispatch_tool", _dispatch)

    events = await _collect(chat_ctx)

    uses = [e for e in events if e["type"] == "tool_use"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert [u["tool_use_id"] for u in uses] == ["toolu_1", "toolu_2"]
    # Every id emitted as a call is answered exactly once.
    assert sorted(r["tool_use_id"] for r in results) == ["toolu_1", "toolu_2"]
    by_id = {r["tool_use_id"]: r["tool"] for r in results}
    assert by_id == {"toolu_1": "get_team_metrics", "toolu_2": "list_action_plans"}


@pytest.mark.asyncio
async def test_every_tool_use_is_answered_exactly_once(chat_ctx, monkeypatch):
    calls = [
        _Final(
            [
                _Block("tool_use", id="toolu_r", name="get_team_metrics", input={}),
                _Block("tool_use", id="toolu_p", name="propose_action_item", input={"title": "x"}),
            ],
            "tool_use",
        ),
        _Final([_Block("text", text="ok")], "end_turn"),
    ]
    _script(_FakeRouter(calls), monkeypatch)

    async def _dispatch(ctx, name, args):
        if name == "propose_action_item":
            return {"proposal_id": str(uuid.uuid4()), "kind": "action_item"}
        return {"ok": True}

    monkeypatch.setattr(linda_agent, "dispatch_tool", _dispatch)

    events = await _collect(chat_ctx)

    asked = {e["tool_use_id"] for e in events if e["type"] == "tool_use"}
    answered = [
        e["tool_use_id"] for e in events if e["type"] in ("tool_result", "proposal")
    ]
    assert asked == {"toolu_r", "toolu_p"}
    assert sorted(answered) == sorted(asked)
    assert len(answered) == len(set(answered))  # exactly once each


def _one(events, type_):
    matches = [e for e in events if e["type"] == type_]
    assert len(matches) == 1, f"expected one {type_} frame, got {len(matches)}"
    return matches[0]


def _async_return(value):
    async def _fn(ctx, name, args):
        return value

    return _fn
