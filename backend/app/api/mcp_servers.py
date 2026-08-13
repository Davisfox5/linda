"""Register external MCP servers as Ask LINDA agent tool sources.

Endpoints (mutations require the ``admin`` role, reads ``manager`` — the same
gating ``integrations_slack.py`` uses for connecting a third-party account,
rather than an API-key scope; connecting a credentialed external system is a
human administrative act):

- GET    /mcp-servers            — registered servers + their discovered tools
- POST   /mcp-servers            — register (or re-register) a server
- POST   /mcp-servers/{name}/refresh — re-run tools/list and re-cache schemas
- DELETE /mcp-servers/{name}     — deregister

Registration performs discovery inline so a bad endpoint or key fails here,
loudly, instead of silently costing the agent its tools at chat time. The
bearer key is Fernet-encrypted at rest in ``Integration.access_token`` and is
never echoed back by any response in this module.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant, require_role
from backend.app.db import get_db
from backend.app.models import Integration, Tenant
from backend.app.services import mcp_tools
from backend.app.services.token_crypto import encrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()


class McpServerCreate(BaseModel):
    name: str = Field(
        ...,
        description=(
            "Short slug identifying the source. Becomes the prefix on every "
            "tool it contributes, e.g. 'flex' -> flex_get_leads."
        ),
    )
    endpoint: str = Field(..., description="Full https URL of the MCP endpoint")
    api_key: str = Field(..., description="Bearer key presented to the server")


class McpToolOut(BaseModel):
    name: str
    exposed_as: str
    description: str


class McpServerOut(BaseModel):
    name: str
    endpoint: str
    discovered_at: Optional[str] = None
    tools: List[McpToolOut]


def _server_out(server: mcp_tools.McpServer) -> McpServerOut:
    return McpServerOut(
        name=server.name,
        endpoint=server.endpoint,
        discovered_at=server.discovered_at,
        tools=[
            McpToolOut(
                name=str(t.get("name") or ""),
                exposed_as=f"{server.prefix}{t.get('name') or ''}",
                description=str(t.get("description") or ""),
            )
            for t in server.tools
        ],
    )


@router.get(
    "/mcp-servers",
    response_model=List[McpServerOut],
    dependencies=[Depends(require_role("manager"))],
)
async def list_mcp_servers(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    servers = await mcp_tools.list_servers(db, tenant.id)
    return [_server_out(s) for s in servers]


@router.post(
    "/mcp-servers",
    response_model=McpServerOut,
    status_code=201,
    dependencies=[Depends(require_role("admin"))],
)
async def register_mcp_server(
    body: McpServerCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Register a server, discovering its tools before storing anything."""
    try:
        name = mcp_tools.validate_server_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    endpoint = body.endpoint.strip()
    if not endpoint.lower().startswith("https://"):
        # The bearer key rides on every call; plaintext transport would leak it.
        raise HTTPException(status_code=422, detail="endpoint must be https://")

    try:
        tools = await mcp_tools.discover_tools(endpoint, body.api_key)
    except mcp_tools.McpError as exc:
        raise HTTPException(status_code=502, detail=f"MCP discovery failed: {exc}")
    if not tools:
        raise HTTPException(
            status_code=502, detail="MCP server advertised no usable tools"
        )

    existing = await _find_row(db, tenant.id, name)
    if existing is None:
        existing = Integration(
            tenant_id=tenant.id,
            provider=mcp_tools.PROVIDER,
            scopes=[],
        )
        db.add(existing)
    existing.access_token = encrypt_token(body.api_key)
    existing.provider_config = mcp_tools.build_config(name, endpoint, tools)
    await db.flush()

    server = mcp_tools.McpServer(
        integration_id=existing.id,
        name=name,
        endpoint=endpoint.rstrip("/"),
        secret="",
        tools=tools,
        discovered_at=(existing.provider_config or {}).get("discovered_at"),
    )
    logger.info(
        "registered MCP tool server %s for tenant %s (%d tools)",
        name, tenant.id, len(tools),
    )
    return _server_out(server)


@router.post(
    "/mcp-servers/{name}/refresh",
    response_model=McpServerOut,
    dependencies=[Depends(require_role("admin"))],
)
async def refresh_mcp_server(
    name: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Re-run discovery. The stored key is reused; it is never sent back out."""
    server = await mcp_tools.get_server(db, tenant.id, name)
    if server is None:
        raise HTTPException(status_code=404, detail=f"no MCP server named '{name}'")

    try:
        tools = await mcp_tools.discover_tools(server.endpoint, server.secret)
    except mcp_tools.McpError as exc:
        raise HTTPException(status_code=502, detail=f"MCP discovery failed: {exc}")
    if not tools:
        raise HTTPException(
            status_code=502, detail="MCP server advertised no usable tools"
        )

    row = await db.get(Integration, server.integration_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no MCP server named '{name}'")
    row.provider_config = mcp_tools.build_config(server.name, server.endpoint, tools)
    await db.flush()

    server.tools = tools
    server.discovered_at = (row.provider_config or {}).get("discovered_at")
    return _server_out(server)


@router.delete(
    "/mcp-servers/{name}",
    status_code=204,
    dependencies=[Depends(require_role("admin"))],
)
async def delete_mcp_server(
    name: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = await _find_row(db, tenant.id, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no MCP server named '{name}'")
    await db.delete(row)
    await db.flush()
    return None


async def _find_row(
    db: AsyncSession, tenant_id: uuid.UUID, name: str
) -> Optional[Integration]:
    stmt = select(Integration).where(
        Integration.tenant_id == tenant_id,
        Integration.provider == mcp_tools.PROVIDER,
    )
    for row in (await db.execute(stmt)).scalars().all():
        if (row.provider_config or {}).get("name") == name:
            return row
    return None
