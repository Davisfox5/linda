"""Tests for the Ask Linda chat API and linda_agent service.

Structure mirrors the existing backend tests: we mock sessions with a
scripted stand-in (no real DB, no real Redis, no real Anthropic) and
assert behaviour of the pieces that matter — tool dispatch, proposal
lifecycle, white-label guard, and the rate-limiter arithmetic.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Small helpers ──────────────────────────────────────────────────────────


def _tenant(is_white_label: bool = False, slug: str = "acme"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme Co",
        slug=slug,
        is_white_label=is_white_label,
    )


def _user():
    return SimpleNamespace(id=uuid.uuid4(), email="sarah@acme.co", name="Sarah M.", role="agent")


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        class _Scalars:
            def __init__(self, value):
                self._v = value
            def all(self):
                return list(self._v) if self._v is not None else []
        return _Scalars(self._value if isinstance(self._value, list) else [])


class _FakeSession:
    """Async-SQLAlchemy-ish session that serves scripted results and records adds."""

    def __init__(self, scripted=None):
        self._scripted = list(scripted or [])
        self._calls = 0
        self.added = []
        self.flushed = False
        self.committed = False

    async def execute(self, stmt):
        if self._calls >= len(self._scripted):
            return _ScalarResult(None)
        result = self._scripted[self._calls]
        self._calls += 1
        return result

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True
        # Assign ids to any added row that doesn't have one (mimicking the DB default).
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                try:
                    obj.id = uuid.uuid4()
                except Exception:
                    pass

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):  # no-op — scripted tests don't need DB state
        return None


# ── Agent context helper ──────────────────────────────────────────────────


def _ctx(session, tenant=None, user=None, conversation_id=None):
    from backend.app.services.linda_agent import AgentContext
    return AgentContext(
        db=session,
        tenant=tenant or _tenant(),
        user=user or _user(),
        conversation_id=conversation_id or uuid.uuid4(),
    )


# ── System prompt ──────────────────────────────────────────────────────────


def test_build_system_blocks_cache_marks_static_portion():
    from backend.app.services.linda_agent import build_system_blocks

    tenant = _tenant(slug="acme")
    user = _user()
    blocks = build_system_blocks(tenant, user)

    assert len(blocks) == 2
    static, dynamic = blocks
    assert static["cache_control"] == {"type": "ephemeral"}
    assert "Linda" in static["text"]
    assert "Listening Intelligence and Natural Dialogue Assistant" in static["text"]
    assert "About the product" in static["text"]
    # Dynamic block must not be cached and carries tenant + user identity.
    assert "cache_control" not in dynamic
    assert "Acme Co" in dynamic["text"]
    assert "acme" in dynamic["text"]
    assert "sarah@acme.co" in dynamic["text"] or "Sarah M." in dynamic["text"]


def test_build_system_blocks_handles_api_key_auth_without_user():
    from backend.app.services.linda_agent import build_system_blocks

    tenant = _tenant()
    blocks = build_system_blocks(tenant, None)
    assert "API key" in blocks[1]["text"]


# ── Tool schema ────────────────────────────────────────────────────────────


def test_tool_schema_exposes_expected_reads_and_drafts():
    from backend.app.services.linda_agent import TOOLS, READ_TOOLS, DRAFT_TOOLS

    names = {t["name"] for t in TOOLS}
    assert names == READ_TOOLS | DRAFT_TOOLS
    assert READ_TOOLS == {
        "search_interactions",
        "get_action_items",
        "get_interaction_detail",
        # Live read of the user's connected Gmail SENT folder (search_sent_email).
        "search_sent_email",
    }
    assert DRAFT_TOOLS == {
        "propose_action_item",
        "propose_email_draft",
        "propose_crm_update",
        # propose_action_plan creates a 1-step Action Plan via the chat
        # endpoint's proposal-execution branch in api/chat.py. Mirrors the
        # action-plans REST surface for Linda-initiated single-step plans.
        "propose_action_plan",
        # propose_queue_bump_email queues the next cold-outreach touch for
        # an enrolled prospect (send stays with the campaign scheduler's
        # window + throttle). Executes in api/chat.py's proposal branch.
        "propose_queue_bump_email",
    }
    for tool in TOOLS:
        assert tool["input_schema"]["type"] == "object"


# ── search_sent_email (live Gmail read) ────────────────────────────────────


def test_search_sent_email_dispatch_returns_messages():
    # dispatch_tool routes search_sent_email to the executor, which offloads the
    # blocking Gmail read to a thread. We patch the sync fetch so no real DB or
    # Gmail is touched, and assert the tool result flows straight back.
    import backend.app.services.linda_agent as la

    ctx = _ctx(_FakeSession())
    fake_payload = {
        "connected": True,
        "provider": "gmail",
        "count": 1,
        "messages": [
            {"to": "cfo@acme.com", "subject": "Pricing follow-up", "date": "Mon, 7 Jul 2026", "snippet": "As discussed…"}
        ],
    }

    captured = {}

    def _fake_sync(tenant_id, query, to, newer_than_days, limit):
        captured.update(
            tenant_id=tenant_id, query=query, to=to, newer_than_days=newer_than_days, limit=limit
        )
        return fake_payload

    with patch.object(la, "_fetch_sent_gmail_sync", _fake_sync):
        result = asyncio.run(
            la.dispatch_tool(
                ctx,
                "search_sent_email",
                {"to": "acme.com", "newer_than_days": 7, "limit": 5},
            )
        )

    assert result == fake_payload
    # Args are coerced and passed through; tenant is scoped to the caller's tenant.
    assert captured["tenant_id"] == ctx.tenant.id
    assert captured["to"] == "acme.com"
    assert captured["newer_than_days"] == 7
    assert captured["limit"] == 5


def test_search_sent_email_limit_is_clamped_and_coerced():
    import backend.app.services.linda_agent as la

    ctx = _ctx(_FakeSession())
    captured = {}

    def _fake_sync(tenant_id, query, to, newer_than_days, limit):
        captured["limit"] = limit
        captured["newer"] = newer_than_days
        return {"connected": True, "messages": []}

    with patch.object(la, "_fetch_sent_gmail_sync", _fake_sync):
        # limit above the cap is clamped to 25; a non-numeric newer_than_days → None.
        asyncio.run(
            la.dispatch_tool(ctx, "search_sent_email", {"limit": 999, "newer_than_days": "soon"})
        )

    assert captured["limit"] == 25
    assert captured["newer"] is None


def test_search_sent_email_not_connected_passes_through():
    import backend.app.services.linda_agent as la

    ctx = _ctx(_FakeSession())
    with patch.object(
        la, "_fetch_sent_gmail_sync", lambda *a, **k: {"connected": False, "provider": "gmail", "messages": []}
    ):
        result = asyncio.run(la.dispatch_tool(ctx, "search_sent_email", {}))
    assert result["connected"] is False


# ── search_interactions (regression: missing `db=` kwarg) ──────────────────
#
# Regression: commit 90ecf8d dropped the `db=ctx.db` kwarg from
# _exec_search_interactions' call into SearchService.search, whose first
# parameter is `db: AsyncSession`. Every search_interactions call then raised
# TypeError: SearchService.search() missing 1 required positional argument:
# 'db', swallowed by the executor's except block as {"error": "search
# failed: ..."}. Guard the argument itself, and bind the captured call
# kwargs against the live signature so a future rename/reorder is caught too.


def test_search_interactions_passes_db_session_to_search_service():
    import inspect

    from backend.app.services.linda_agent import dispatch_tool
    from backend.app.services.search_service import SearchService

    session = _FakeSession()
    ctx = _ctx(session)
    captured = {}

    async def _fake_search(self, **kwargs):
        captured.update(kwargs)
        return []

    with patch.object(SearchService, "search", _fake_search):
        result = asyncio.run(
            dispatch_tool(ctx, "search_interactions", {"query": "acme pricing"})
        )

    assert "error" not in result
    assert captured["db"] is ctx.db
    assert captured["tenant_id"] == str(ctx.tenant.id)
    assert captured["query"] == "acme pricing"

    # Binds cleanly against SearchService.search's real signature — fails if
    # the executor and the service signature drift apart again.
    sig = inspect.signature(SearchService.search)
    sig.bind(self=SearchService(), **captured)


# ── Tool dispatch: draft tools create proposals, do not mutate ────────────


def test_propose_action_item_creates_pending_proposal():
    from backend.app.services.linda_agent import dispatch_tool, WriteProposal

    session = _FakeSession()
    ctx = _ctx(session)

    result = asyncio.run(
        dispatch_tool(ctx, "propose_action_item", {
            "title": "Follow up with Acme",
            "description": "They asked about enterprise tier pricing.",
            "assignee_email": "sarah@acme.co",
            "due_date": "2026-04-25",
            "priority": "high",
        })
    )

    assert result["kind"] == "action_item"
    assert result["status"] == "pending"
    assert result["preview"]["title"] == "Follow up with Acme"
    # A WriteProposal row was added, not an ActionItem.
    kinds = [type(o).__name__ for o in session.added]
    assert "WriteProposal" in kinds
    assert "ActionItem" not in kinds
    # Confirm the added WriteProposal matches the tool call.
    wp = next(o for o in session.added if type(o).__name__ == "WriteProposal")
    assert wp.kind == "action_item"
    assert wp.status == "pending"
    assert wp.expires_at > datetime.now(timezone.utc)


def test_unknown_tool_returns_error():
    from backend.app.services.linda_agent import dispatch_tool

    result = asyncio.run(dispatch_tool(_ctx(_FakeSession()), "bogus_tool", {}))
    assert "error" in result


# ── History rehydration: tool_use turns must replay with their tool_result ──
#
# Regression: `_load_history` dropped every `role="tool"` row, so a conversation
# whose first turn used a tool replayed the assistant's tool_use blocks with no
# matching tool_result. Turn 2 then 400s with
#   "tool_use ids were found without tool_result blocks immediately after".
# History must reconstruct each tool_use immediately followed by its tool_result.


def _msg(role, content=None, tool_calls=None):
    return SimpleNamespace(role=role, content=content or "", tool_calls=tool_calls)


def _assert_tool_pairs_valid(messages):
    """Every assistant tool_use id is answered by the very next user message."""
    for idx, m in enumerate(messages):
        content = m["content"]
        use_ids = [
            b["id"]
            for b in (content if isinstance(content, list) else [])
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if not use_ids:
            continue
        assert idx + 1 < len(messages), "tool_use turn has no following message"
        nxt = messages[idx + 1]
        assert nxt["role"] == "user"
        result_ids = {
            b.get("tool_use_id")
            for b in nxt["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        assert set(use_ids) == result_ids


def test_load_history_replays_tool_result_after_tool_use():
    from backend.app.services.linda_agent import _load_history

    conv_id = uuid.uuid4()
    # A completed tool-using turn as persisted by run_chat_turn: user →
    # assistant(tool_use) → tool(tool_result) → assistant(text).
    rows = [
        _msg("user", content="Who are my prospects?"),
        _msg(
            "assistant",
            tool_calls=[
                {"type": "tool_use", "id": "toolu_1", "name": "search_interactions", "input": {"q": "prospects"}},
            ],
        ),
        _msg(
            "tool",
            tool_calls=[
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "[]"},
            ],
        ),
        _msg("assistant", content="You have three active prospects."),
    ]
    session = _FakeSession(scripted=[_ScalarResult(rows)])

    history = asyncio.run(_load_history(session, conv_id))

    # The tool_result turn survived rehydration and pairs with the tool_use.
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant", "user", "assistant"]
    _assert_tool_pairs_valid(history)


def test_repair_tool_pairs_synthesizes_missing_tool_result():
    # Defensive: an interrupted turn persisted the assistant tool_use but never
    # the tool row. Replaying the orphan verbatim would 400; repair it instead.
    from backend.app.services.linda_agent import _repair_tool_pairs

    orphaned = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_x", "name": "search_interactions", "input": {}}],
        },
    ]
    repaired = _repair_tool_pairs(orphaned)
    _assert_tool_pairs_valid(repaired)
    assert repaired[-1]["role"] == "user"
    assert repaired[-1]["content"][0]["tool_use_id"] == "toolu_x"


def test_next_created_at_is_strictly_increasing():
    # All rows in a turn share one DB transaction, so func.now() would stamp
    # them identically and replay order (ORDER BY created_at) would be undefined.
    # _next_created_at must advance even when wall-clock returns the same instant.
    from backend.app.services.linda_agent import _next_created_at

    t0 = _next_created_at()
    t1 = _next_created_at(t0)
    t2 = _next_created_at(t1)
    assert t0 < t1 < t2


def test_run_chat_turn_persists_rows_in_strict_time_order():
    # End-to-end: a tool-using turn must write user < assistant < tool < assistant
    # by created_at, so the next turn's _load_history replays a valid pairing.
    import backend.app.services.linda_agent as la

    session = _FakeSession(scripted=[_ScalarResult([])])  # empty prior history
    ctx = _ctx(session)

    # A fake streaming turn: first call emits a tool_use, second call finishes.
    calls = {"n": 0}

    class _FakeStream:
        def __init__(self, final):
            self._final = final
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def __aiter__(self):
            async def _gen():
                if False:
                    yield None
            return _gen()
        async def get_final_message(self):
            return self._final

    def _block(**kw):
        return SimpleNamespace(**kw)

    def _fake_astream(self, req):
        calls["n"] += 1
        if calls["n"] == 1:
            final = SimpleNamespace(
                stop_reason="tool_use",
                content=[_block(type="tool_use", id="toolu_a", name="get_action_items", input={})],
            )
        else:
            final = SimpleNamespace(
                stop_reason="end_turn",
                content=[_block(type="text", text="Here you go.")],
            )
        return _FakeStream(final)

    async def _fake_dispatch(ctx, name, args):
        return {"items": []}

    with patch.object(la, "get_async_anthropic", lambda: MagicMock()), \
         patch.object(la.ModelRouter, "astream", _fake_astream), \
         patch.object(la, "dispatch_tool", _fake_dispatch):
        async def _run():
            return [ev async for ev in la.run_chat_turn(ctx, "what's open?")]
        asyncio.run(_run())

    rows = [o for o in session.added if type(o).__name__ == "LindaChatMessage"]
    roles = [r.role for r in rows]
    assert roles == ["user", "assistant", "tool", "assistant"]
    times = [r.created_at for r in rows]
    assert times == sorted(times) and len(set(times)) == len(times), (
        "created_at must be strictly increasing so replay order is deterministic"
    )


def test_repair_tool_pairs_drops_dangling_tool_result():
    from backend.app.services.linda_agent import _repair_tool_pairs

    dangling = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_gone", "content": "{}"}]},
        {"role": "assistant", "content": "hello"},
    ]
    repaired = _repair_tool_pairs(dangling)
    # No message may contain a tool_result that isn't answering a tool_use.
    for m in repaired:
        if isinstance(m["content"], list):
            for b in m["content"]:
                assert not (isinstance(b, dict) and b.get("type") == "tool_result")


# ── Rate limiter arithmetic ────────────────────────────────────────────────


def test_rate_limiter_allows_under_limit_and_blocks_over():
    from backend.app.services.chat_rate_limiter import LindaRateLimiter

    limiter = LindaRateLimiter(limit=3, window_seconds=60)

    # Fake Redis pipeline: first two calls yield counts 1 and 2, the fourth returns 4 (blocked).
    counts = iter([1, 2, 3, 4])

    class _FakePipe:
        def __init__(self):
            self.ops = []
        def incr(self, key):
            self.ops.append(("incr", key))
            return self
        def expire(self, key, ttl):
            self.ops.append(("expire", key, ttl))
            return self
        async def execute(self):
            return [next(counts), True]

    class _FakeRedis:
        def pipeline(self):
            return _FakePipe()

    limiter._redis = _FakeRedis()

    async def _go():
        return [await limiter.check("tenant-1") for _ in range(4)]

    results = asyncio.run(_go())
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[3].retry_after_s > 0


# ── Proposal lifecycle via the HTTP endpoints ──────────────────────────────


def _proposal(kind="action_item", status="pending", expires_at=None, payload=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=None,
        kind=kind,
        payload=payload or {"title": "Follow up", "interaction_id": str(uuid.uuid4())},
        status=status,
        resulting_entity_id=None,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=24)),
        confirmed_at=None,
    )


def test_confirm_expired_proposal_returns_410():
    # Patch the model imports inside chat.py to use lightweight namespaces.
    from backend.app.api import chat as chat_module
    from fastapi import HTTPException

    tenant = _tenant()
    expired = _proposal(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))

    session = _FakeSession(scripted=[_ScalarResult(expired)])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_module.confirm_proposal(expired.id, tenant=tenant, db=session))
    assert exc.value.status_code == 410
    # Status was flipped to "expired" before the exception.
    assert expired.status == "expired"


def test_confirm_pending_action_item_creates_row_and_marks_confirmed():
    from backend.app.api import chat as chat_module

    tenant = _tenant()
    interaction_id = uuid.uuid4()
    pending = _proposal(payload={
        "title": "Call back Acme",
        "description": "They want the enterprise quote.",
        "interaction_id": str(interaction_id),
        "assignee_email": "sarah@acme.co",
        "due_date": "2026-04-25",
        "priority": "high",
    })

    # First execute: fetch the proposal. Second: resolve assignee by email.
    assignee_id = uuid.uuid4()
    session = _FakeSession(scripted=[
        _ScalarResult(pending),
        _ScalarResult(assignee_id),
    ])

    result = asyncio.run(chat_module.confirm_proposal(pending.id, tenant=tenant, db=session))
    assert pending.status == "confirmed"
    assert pending.confirmed_at is not None
    # An ActionItem row was queued on the session.
    kinds = [type(o).__name__ for o in session.added]
    assert "ActionItem" in kinds
    assert session.committed is True


def test_cancel_pending_proposal_marks_cancelled():
    from backend.app.api import chat as chat_module

    tenant = _tenant()
    pending = _proposal()
    session = _FakeSession(scripted=[_ScalarResult(pending)])

    asyncio.run(chat_module.cancel_proposal(pending.id, tenant=tenant, db=session))
    assert pending.status == "cancelled"
    # Cancel does not fire the mutator.
    assert all(type(o).__name__ != "ActionItem" for o in session.added)


def test_double_confirm_returns_409():
    from backend.app.api import chat as chat_module
    from fastapi import HTTPException

    tenant = _tenant()
    already_done = _proposal(status="confirmed")
    session = _FakeSession(scripted=[_ScalarResult(already_done)])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_module.confirm_proposal(already_done.id, tenant=tenant, db=session))
    assert exc.value.status_code == 409


# ── SSE stream termination guarantees ──────────────────────────────────────
#
# _stream_chat must end every stream with a terminal `done`/`error` event,
# emit keep-alive comments while the producer is silent, and bound total
# stream lifetime server-side (Flex aborts client-side at 120s; we must
# terminate cleanly first).


class _StreamSession(_FakeSession):
    def __init__(self):
        super().__init__()
        self.rolled_back = False

    async def rollback(self):
        self.rolled_back = True


def _drain_stream(ctx, message="hi"):
    from backend.app.api import chat as chat_module

    async def _go():
        frames = []
        async for frame in chat_module._stream_chat(ctx, message, uuid.uuid4()):
            frames.append(frame)
        return frames

    return asyncio.run(_go())


def _parse_events(frames):
    import json as _json

    return [
        _json.loads(f[len("data: "):])
        for f in frames
        if f.startswith("data: ")
    ]


def test_stream_chat_passes_events_through_and_ends_with_done(monkeypatch):
    from backend.app.api import chat as chat_module

    async def fake_turn(ctx, msg):
        yield {"type": "text", "delta": "hello"}
        yield {"type": "done"}

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_turn)
    session = _StreamSession()
    frames = _drain_stream(SimpleNamespace(db=session))
    events = _parse_events(frames)

    assert events[0]["type"] == "conversation"
    assert {"type": "text", "delta": "hello"} in events
    assert events[-1]["type"] == "done"
    assert session.committed is True
    assert session.rolled_back is False


def test_stream_chat_emits_terminal_error_when_producer_raises(monkeypatch):
    from backend.app.api import chat as chat_module

    async def failing_turn(ctx, msg):
        yield {"type": "text", "delta": "partial"}
        raise RuntimeError("upstream LLM blew up")

    monkeypatch.setattr(chat_module, "run_chat_turn", failing_turn)
    session = _StreamSession()
    events = _parse_events(_drain_stream(SimpleNamespace(db=session)))

    assert events[-1]["type"] == "error"
    assert "upstream LLM blew up" in events[-1]["message"]
    assert session.rolled_back is True
    assert session.committed is False


def test_stream_chat_bounds_lifetime_with_terminal_error(monkeypatch):
    from backend.app.api import chat as chat_module

    monkeypatch.setattr(chat_module, "_STREAM_LIFETIME_S", 0.3)
    monkeypatch.setattr(chat_module, "_HEARTBEAT_INTERVAL_S", 0.05)

    async def stalled_turn(ctx, msg):
        yield {"type": "text", "delta": "thinking…"}
        await asyncio.sleep(60)  # upstream stall — never finishes
        yield {"type": "done"}

    monkeypatch.setattr(chat_module, "run_chat_turn", stalled_turn)
    session = _StreamSession()
    frames = _drain_stream(SimpleNamespace(db=session))
    events = _parse_events(frames)

    # Terminal error, not a silent hang.
    assert events[-1]["type"] == "error"
    assert session.rolled_back is True
    assert session.committed is False
    # Heartbeat comments were emitted while waiting on the stall.
    assert any(f.startswith(": keep-alive") for f in frames)


def test_stream_chat_heartbeats_during_slow_turn_then_completes(monkeypatch):
    from backend.app.api import chat as chat_module

    monkeypatch.setattr(chat_module, "_HEARTBEAT_INTERVAL_S", 0.05)

    async def slow_turn(ctx, msg):
        await asyncio.sleep(0.2)  # several heartbeat intervals of silence
        yield {"type": "text", "delta": "worth the wait"}
        yield {"type": "done"}

    monkeypatch.setattr(chat_module, "run_chat_turn", slow_turn)
    session = _StreamSession()
    frames = _drain_stream(SimpleNamespace(db=session))
    events = _parse_events(frames)

    assert any(f.startswith(": keep-alive") for f in frames)
    assert events[-1]["type"] == "done"
    assert session.committed is True


# ── White-label guard ──────────────────────────────────────────────────────


def test_white_label_tenant_gets_404_on_ping_confirm_cancel():
    from backend.app.api import chat as chat_module
    from fastapi import HTTPException

    wl_tenant = _tenant(is_white_label=True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_module.chat_ping(tenant=wl_tenant))
    assert exc.value.status_code == 404

    session = _FakeSession()
    with pytest.raises(HTTPException) as exc2:
        asyncio.run(chat_module.confirm_proposal(uuid.uuid4(), tenant=wl_tenant, db=session))
    assert exc2.value.status_code == 404

    with pytest.raises(HTTPException) as exc3:
        asyncio.run(chat_module.cancel_proposal(uuid.uuid4(), tenant=wl_tenant, db=session))
    assert exc3.value.status_code == 404


# ── get_or_create_conversation against a real mapper ──────────────────────
#
# Regression: LindaChatConversation was an id-only stub for a while (the DB
# table had tenant_id/user_id/title from migration a1b2c3d4e5f6 but the
# model didn't), so this function raised AttributeError in production.
# Runs against the shared SQLite fixtures — a scripted-mock session would
# not have caught the mapper drift.


@pytest.mark.asyncio
async def test_get_or_create_conversation_round_trips(test_session, test_tenant):
    from backend.app.services.linda_agent import get_or_create_conversation

    convo = await get_or_create_conversation(
        test_session, test_tenant, None, conversation_id=None
    )
    await test_session.commit()
    assert convo.tenant_id == test_tenant.id

    again = await get_or_create_conversation(
        test_session, test_tenant, None, conversation_id=convo.id
    )
    assert again.id == convo.id


@pytest.mark.asyncio
async def test_get_or_create_conversation_ignores_foreign_tenants_id(
    test_session, test_tenant
):
    from backend.app.models import Tenant
    from backend.app.services.linda_agent import get_or_create_conversation

    other = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    test_session.add(other)
    await test_session.commit()

    theirs = await get_or_create_conversation(
        test_session, other, None, conversation_id=None
    )
    await test_session.commit()

    # Asking for their conversation id under OUR tenant must not return it.
    mine = await get_or_create_conversation(
        test_session, test_tenant, None, conversation_id=theirs.id
    )
    assert mine.id != theirs.id
    assert mine.tenant_id == test_tenant.id
