"""Shared fixtures for Teams compliance-recording tests.

Mirrors ``tests/test_uc_common.py``'s shape: a focused FastAPI app that
mounts only the ``teams_recording`` router with a real (SQLite,
in-memory) ``get_db`` override, plus a helper that seeds a
``teams_compliance`` ``Integration`` row so notification/bot-callback
tests can exercise the actual tenant-resolution + persistence path
instead of only the "unknown tenant" skip branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

FIXTURES = Path(__file__).parent / "fixtures" / "teams"

# Matches the tenantId in tests/fixtures/teams/notification_*.json.
AAD_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def load_fixture(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest_asyncio.fixture
async def teams_test_app(test_session_factory):
    """FastAPI app with only the teams-recording router mounted, plus a
    real (SQLite) DB override — the notification/bot-callback endpoints
    now write real rows, unlike the no-DB scaffold round."""
    from backend.app.api.teams_recording import router as teams_router
    from backend.app.db import get_db

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(teams_router, prefix="/api/v1", tags=["teams-recording"])
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def teams_test_client(teams_test_app):
    transport = ASGITransport(app=teams_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def seeded_teams_integration(test_session_factory, test_tenant):
    """Seed a ``teams_compliance`` Integration row for ``test_tenant``,
    mapped to :data:`AAD_TENANT_ID` — the ``tenantId`` baked into the
    Graph notification fixtures."""
    from backend.app.models import Integration
    from backend.app.services.token_crypto import encrypt_token

    async with test_session_factory() as session:
        integ = Integration(
            tenant_id=test_tenant.id,
            provider="teams_compliance",
            access_token=encrypt_token("app-only-graph-auth-fixture"),
            provider_config={"aad_tenant_id": AAD_TENANT_ID},
        )
        session.add(integ)
        await session.commit()
        await session.refresh(integ)
        return integ
