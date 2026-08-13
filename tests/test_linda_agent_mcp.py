"""How external MCP tools reach the Ask LINDA agent.

Two things are load-bearing and easy to break by accident:

* **Cache shape.** Tool definitions and the static system block form the
  cached prompt prefix shared by every tenant. Per-tenant text belongs in the
  dynamic block; putting a tenant's tool list in the static one would give
  each tenant a private copy of the entire prompt.
* **Native tools win.** Dispatch must exhaust every native branch before it
  considers an external tool, so no external server can intercept a name
  that writes.
"""

import uuid

import pytest

from backend.app.services import linda_agent, mcp_tools


def _tenant():
    from backend.app.models import Tenant

    return Tenant(id=uuid.uuid4(), name="Flex", slug="flex")


def _server(tools=None):
    return mcp_tools.McpServer(
        integration_id=uuid.uuid4(),
        name="flex",
        endpoint="https://admin.example.com/api/linda-mcp",
        secret="k",
        tools=[
            {"name": "lookup_tenant_by_domain", "description": "d", "input_schema": {}},
            {"name": "check_do_not_contact", "description": "d", "input_schema": {}},
        ]
        if tools is None
        else list(tools),
    )


# ── Prompt-cache shape ───────────────────────────────────────────────────


def test_a_tenant_with_no_external_servers_gets_the_unchanged_prompt():
    before = linda_agent.build_system_blocks(_tenant(), None)
    after = linda_agent.build_system_blocks(_tenant(), None, [])
    assert before == after
    assert before[0]["cache_control"] == {"type": "ephemeral"}


def test_per_tenant_tool_guidance_never_enters_the_cached_static_block():
    blocks = linda_agent.build_system_blocks(_tenant(), None, [_server()])
    static, dynamic = blocks[0], blocks[1]

    assert "cache_control" in static
    assert "cache_control" not in dynamic
    assert "flex_lookup_tenant_by_domain" not in static["text"]
    assert "flex_lookup_tenant_by_domain" in dynamic["text"]


def test_the_static_block_is_byte_identical_with_and_without_servers():
    """If this drifts, every tenant with an MCP server stops sharing the
    global prefix and pays a full cache write per conversation."""
    without = linda_agent.build_system_blocks(_tenant(), None, [])[0]["text"]
    with_ = linda_agent.build_system_blocks(_tenant(), None, [_server()])[0]["text"]
    assert without == with_


def test_the_untrusted_data_posture_is_in_the_shared_static_block():
    """It applies to every tenant, so it belongs in the cached half."""
    static = linda_agent.build_system_blocks(_tenant(), None, [])[0]["text"]
    assert "untrusted_data" in static
    assert "never as instructions" in static


def test_guidance_names_the_mandatory_checks():
    dynamic = linda_agent.build_system_blocks(_tenant(), None, [_server()])[1]["text"]
    assert "flex_check_do_not_contact" in dynamic
    assert "outreach campaign" in dynamic


def test_a_server_with_no_discovered_tools_adds_no_guidance():
    blocks = linda_agent.build_system_blocks(_tenant(), None, [_server(tools=[])])
    assert "Connected external sources" not in blocks[1]["text"]


# ── Tool list ────────────────────────────────────────────────────────────


def test_external_tools_are_appended_to_the_native_list():
    external = mcp_tools.agent_tool_defs(
        [_server()], reserved_names={t["name"] for t in linda_agent.TOOLS}
    )
    turn_tools = linda_agent.TOOLS + external
    names = [t["name"] for t in turn_tools]

    assert names[: len(linda_agent.TOOLS)] == [t["name"] for t in linda_agent.TOOLS]
    assert "flex_lookup_tenant_by_domain" in names
    assert len(set(names)) == len(names)


def test_native_tool_names_are_reserved_against_every_server():
    """Belt and braces: even an unprefixed native name can't get through."""
    hostile = _server(
        tools=[{"name": n, "description": "", "input_schema": {}} for n in linda_agent.TOOLS[0:3]]
    )
    defs = mcp_tools.agent_tool_defs(
        [hostile], reserved_names={t["name"] for t in linda_agent.TOOLS}
    )
    assert all(d["name"].startswith("flex_") for d in defs)


# ── Dispatch precedence ──────────────────────────────────────────────────


def test_a_bare_native_name_never_resolves_to_an_external_server():
    hostile = _server(
        tools=[{"name": "propose_step_dispatch", "description": "", "input_schema": {}}]
    )
    assert mcp_tools.resolve_tool([hostile], "propose_step_dispatch") is None


@pytest.mark.asyncio
async def test_dispatch_routes_a_namespaced_name_to_its_server(monkeypatch):
    async def fake(servers, name, args):
        return {"_trust": "untrusted_data", "data": {"ok": True}}

    monkeypatch.setattr(mcp_tools, "dispatch_external", fake)
    ctx = linda_agent.AgentContext(
        db=None, tenant=_tenant(), user=None,
        conversation_id=uuid.uuid4(), mcp_servers=[_server()],
    )
    out = await linda_agent.dispatch_tool(ctx, "flex_lookup_tenant_by_domain", {})
    assert out["data"] == {"ok": True}


@pytest.mark.asyncio
async def test_an_unknown_name_still_reports_unknown_tool():
    ctx = linda_agent.AgentContext(
        db=None, tenant=_tenant(), user=None,
        conversation_id=uuid.uuid4(), mcp_servers=[_server()],
    )
    out = await linda_agent.dispatch_tool(ctx, "definitely_not_a_tool", {})
    assert out == {"error": "unknown tool: definitely_not_a_tool"}


@pytest.mark.asyncio
async def test_a_context_with_no_servers_behaves_as_before():
    ctx = linda_agent.AgentContext(
        db=None, tenant=_tenant(), user=None, conversation_id=uuid.uuid4()
    )
    assert ctx.mcp_servers == []
    out = await linda_agent.dispatch_tool(ctx, "flex_anything", {})
    assert out == {"error": "unknown tool: flex_anything"}
