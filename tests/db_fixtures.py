"""In-memory SQLite fixtures for DB-backed integration tests.

CI doesn't run a Postgres service, and the integration tests we need
(outcomes idempotency, dead-letter, HMAC flow) don't touch
Postgres-specific SQL — they use ORM operations and a unique index that
SQLite supports natively.  So we use ``aiosqlite`` in-memory.

Fixtures:

- ``test_engine`` — async SQLite engine with every model's table created.
- ``test_session`` — a per-test async session wired to that engine.
- ``test_app`` — the FastAPI app with ``get_db`` overridden to use the
  test engine, and ``get_current_tenant`` bypassed to return a seeded
  tenant without needing an API key round-trip.
- ``test_tenant`` — the seeded :class:`Tenant` row (tests use its id).
- ``test_interaction`` — a seeded :class:`Interaction` + matching
  :class:`InteractionFeatures` row so outcome events have something
  real to attach to.
- ``test_client`` — ``httpx.AsyncClient`` against ``test_app`` via ASGI
  transport.  No network port bound.
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


# SQLite can't compile Postgres-specific types. Teach it to render JSONB
# as JSON and UUID as CHAR(36) so ``Base.metadata.create_all`` succeeds.
# These only fire on the sqlite dialect — Postgres paths are untouched.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


# Run each test in its own asyncio task-local loop so the in-memory DB
# is completely isolated per test.
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def test_engine():
    # Models register on ``Base`` at import time — import late so the
    # conftest env vars have already been applied.
    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers every mapped class

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def test_session_factory(test_engine):
    return async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest_asyncio.fixture
async def test_session(test_session_factory) -> AsyncIterator[AsyncSession]:
    async with test_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def test_tenant(test_session_factory):
    """Seed one tenant row; return the Tenant instance."""
    from backend.app.models import Tenant

    async with test_session_factory() as session:
        tenant = Tenant(
            name="Test Tenant",
            slug=f"t-{uuid.uuid4().hex[:8]}",
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


@pytest_asyncio.fixture
async def test_interaction(test_session_factory, test_tenant):
    """Seed an interaction + matching InteractionFeatures row.

    Outcome events must attach to an interaction that belongs to the
    calling tenant; if no features row exists the endpoint treats it as
    ``interaction_not_found``.
    """
    from backend.app.models import Interaction, InteractionFeatures

    async with test_session_factory() as session:
        interaction = Interaction(
            tenant_id=test_tenant.id,
            channel="voice",
        )
        session.add(interaction)
        await session.commit()
        await session.refresh(interaction)

        features = InteractionFeatures(
            interaction_id=interaction.id,
            tenant_id=test_tenant.id,
        )
        session.add(features)
        await session.commit()
        return interaction


@pytest_asyncio.fixture
async def test_app(test_session_factory, test_tenant):
    """Minimal FastAPI app hosting the outcomes router only.

    We mount just the endpoints under test rather than ``backend.app.main.app``
    because the full app hard-imports optional deps (elasticsearch, etc.)
    at module load.  The integration tests only exercise outcomes, so a
    focused test app is both faster and more portable.
    """
    from fastapi import FastAPI

    from backend.app.auth import get_current_principal, get_current_tenant
    from backend.app.db import get_db
    from backend.app.api.outcomes import router as outcomes_router
    from backend.app.api.ws_tickets import router as ws_tickets_router

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_get_tenant():
        # Re-fetch from the DB so tests that mutate the tenant row
        # (e.g., to set ``outcomes_hmac_secret``) see the latest state.
        from backend.app.models import Tenant
        from sqlalchemy import select

        async with test_session_factory() as s:
            result = await s.execute(
                select(Tenant).where(Tenant.id == test_tenant.id)
            )
            return result.scalar_one()

    async def _override_get_principal():
        # Human dashboard principal (not api_key) so endpoints that
        # resolve get_current_principal directly — e.g. /ws/tickets —
        # skip the API-key entitlement/scope gates, mirroring the
        # legacy get_current_tenant bypass above.
        from backend.app.auth import AuthPrincipal

        tenant = await _override_get_tenant()
        return AuthPrincipal(
            tenant=tenant,
            user=None,
            role="admin",
            source="clerk",
        )

    app = FastAPI()
    app.include_router(outcomes_router, prefix="/api/v1", tags=["outcomes"])
    app.include_router(ws_tickets_router, prefix="/api/v1", tags=["ws-tickets"])
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_tenant] = _override_get_tenant
    app.dependency_overrides[get_current_principal] = _override_get_principal
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def test_client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
