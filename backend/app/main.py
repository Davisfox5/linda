"""LINDA — FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from backend.app.config import get_settings
from backend.app.db import engine
from backend.app.logging_setup import (
    bind_context,
    configure_logging,
    new_request_id,
    reset_context,
)
from backend.app.observability import init_sentry

configure_logging()
init_sentry()

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    # ── Startup ──
    # DB tables are managed by Alembic, but we verify connectivity
    async with engine.begin() as conn:
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))

    # RLS posture check: if the runtime connection is the table owner (or
    # holds BYPASSRLS), every tenant-isolation policy is silently skipped.
    # Expected in dev; in staging/production it means APP_DATABASE_URL
    # wasn't set and the backstop is OFF. Warn loudly — never block boot.
    try:
        from backend.app.rls import runtime_bypasses_rls

        async with engine.connect() as conn:
            bypass_reason = await conn.run_sync(runtime_bypasses_rls)
        if bypass_reason:
            logging.getLogger("backend.app.main").warning(
                "RLS BACKSTOP INACTIVE: %s — the app is connecting with a "
                "role that bypasses row-level security. Set APP_DATABASE_URL "
                "to the non-owner linda_app role's DSN.",
                bypass_reason,
            )
    except Exception:  # pragma: no cover — posture check must never block boot
        pass

    # SIPREC idle reaper: finalises sessions whose Redis state
    # expired because the SRS died without recording.stopped. Runs in
    # every worker; reaping is idempotent so the overlap is harmless.
    import asyncio as _asyncio

    async def _siprec_reap_loop() -> None:
        from backend.app.services.telephony.siprec.bridge import get_bridge

        log = logging.getLogger(__name__)
        while True:
            await _asyncio.sleep(60.0)
            try:
                reaped = await get_bridge().reap_stale_sessions()
                if reaped:
                    log.warning("SIPREC reaper finalised %d stale sessions", reaped)
            except Exception:
                log.exception("SIPREC reap loop iteration failed")

    reap_task = _asyncio.get_event_loop().create_task(_siprec_reap_loop())

    yield

    # ── Shutdown ──
    reap_task.cancel()
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id + tenant id to every request.

    * If the caller sends ``X-Request-Id``, we honor it (lets upstream
      load balancers and SDK clients correlate their own trace). Else
      we mint a fresh UUID4.
    * The tenant id is populated opportunistically from the
      ``X-Tenant-Id`` header if present; the auth layer later updates
      it with the authenticated tenant's id. Either way, every log line
      in the request's async scope gets the tenant id for free.
    * The request id echoes on the response as ``X-Request-Id`` so
      clients can report "this call failed, here's my request id" and
      we can find it instantly.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or new_request_id()
        tenant_hint = request.headers.get("X-Tenant-Id")
        tokens = bind_context(request_id=request_id, tenant_id=tenant_hint)
        try:
            response = await call_next(request)
        finally:
            reset_context(tokens)
        response.headers["X-Request-Id"] = request_id
        return response


app.add_middleware(RequestContextMiddleware)

# ── CORS ──────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)

# ── API Routers ───────────────────────────────────────────
from backend.app.api.interactions import router as interactions_router  # noqa: E402
from backend.app.api.health import router as health_router  # noqa: E402
from backend.app.api.api_keys import router as api_keys_router  # noqa: E402
from backend.app.api.call_library import router as call_library_router  # noqa: E402
from backend.app.api.search import router as search_router  # noqa: E402
from backend.app.api.comments import router as comments_router  # noqa: E402
from backend.app.api.contacts import router as contacts_router  # noqa: E402
from backend.app.api.analytics import router as analytics_router  # noqa: E402
from backend.app.api.knowledge_base import router as kb_router  # noqa: E402
from backend.app.api.scorecards import router as scorecards_router  # noqa: E402
from backend.app.api.action_items import router as action_items_router  # noqa: E402
from backend.app.api.action_plans import router as action_plans_router  # noqa: E402
from backend.app.api.notifications import router as notifications_router  # noqa: E402
from backend.app.api.manager import router as manager_router  # noqa: E402
from backend.app.api.integrations_slack import router as integrations_slack_router  # noqa: E402
from backend.app.api.profiles import router as profiles_router  # noqa: E402
from backend.app.api.outcomes import router as outcomes_router  # noqa: E402
from backend.app.api.corrections import router as corrections_router  # noqa: E402
from backend.app.api.quality import router as quality_router  # noqa: E402
from backend.app.api.ws_tickets import router as ws_tickets_router  # noqa: E402
from backend.app.api.live_sessions import router as live_sessions_router  # noqa: E402
from backend.app.api.coaching import router as coaching_router  # noqa: E402
from backend.app.api.oauth import router as oauth_router  # noqa: E402
from backend.app.api.conversations import router as conversations_router  # noqa: E402
from backend.app.api.webhooks import router as webhooks_router  # noqa: E402
from backend.app.api.email_push import router as email_push_router  # noqa: E402
from backend.app.api.email_backfill import router as email_backfill_router  # noqa: E402
from backend.app.api.feedback import router as feedback_router  # noqa: E402
from backend.app.api.evaluation import router as evaluation_router  # noqa: E402
from backend.app.api.experiments import router as experiments_router  # noqa: E402
from backend.app.api.campaigns import router as campaigns_router  # noqa: E402
from backend.app.api.outreach import router as outreach_router  # noqa: E402
from backend.app.api.outreach_links import router as outreach_links_router  # noqa: E402
from backend.app.api.admin import router as admin_router  # noqa: E402
from backend.app.api.admin_motions import router as admin_motions_router  # noqa: E402
from backend.app.api.support import router as support_router  # noqa: E402
from backend.app.api.support_csat import router as support_csat_router  # noqa: E402
from backend.app.api.cs import router as cs_router  # noqa: E402
from backend.app.api.kb_requests import router as kb_requests_router  # noqa: E402
from backend.app.api.sso_provisioning import router as sso_router  # noqa: E402
from backend.app.api.scim import router as scim_router  # noqa: E402
from backend.app.api.customer_memory import router as customer_memory_router  # noqa: E402
from backend.app.api.auth_session import router as auth_session_router  # noqa: E402
from backend.app.api.crm import router as crm_router  # noqa: E402
from backend.app.api.emails import router as emails_router  # noqa: E402
from backend.app.api.onboarding import router as onboarding_router  # noqa: E402
from backend.app.api.stripe_webhook import router as stripe_router  # noqa: E402
from backend.app.api.telephony import router as telephony_router  # noqa: E402
from backend.app.api.chat import router as chat_router  # noqa: E402
from backend.app.api.me import router as me_router  # noqa: E402
from backend.app.api.signup import router as signup_router  # noqa: E402

app.include_router(health_router, prefix=settings.API_V1_PREFIX, tags=["health"])
app.include_router(interactions_router, prefix=settings.API_V1_PREFIX, tags=["interactions"])
app.include_router(api_keys_router, prefix=settings.API_V1_PREFIX, tags=["api-keys"])
app.include_router(call_library_router, prefix=settings.API_V1_PREFIX, tags=["library"])
app.include_router(search_router, prefix=settings.API_V1_PREFIX, tags=["search"])
app.include_router(comments_router, prefix=settings.API_V1_PREFIX, tags=["comments"])
app.include_router(contacts_router, prefix=settings.API_V1_PREFIX, tags=["contacts"])
app.include_router(scorecards_router, prefix=settings.API_V1_PREFIX, tags=["scorecards"])
app.include_router(analytics_router, prefix=settings.API_V1_PREFIX, tags=["analytics"])
app.include_router(kb_router, prefix=settings.API_V1_PREFIX, tags=["knowledge-base"])
from fastapi import Depends as _Depends  # noqa: E402
from backend.app.auth import require_role as _require_role  # noqa: E402

app.include_router(action_items_router, prefix=settings.API_V1_PREFIX, tags=["action-items"])
app.include_router(action_plans_router, prefix=settings.API_V1_PREFIX, tags=["action-plans"])
app.include_router(notifications_router, prefix=settings.API_V1_PREFIX, tags=["notifications"])
app.include_router(manager_router, prefix=settings.API_V1_PREFIX, tags=["manager"])
app.include_router(integrations_slack_router, prefix=settings.API_V1_PREFIX, tags=["integrations-slack"])
app.include_router(profiles_router, prefix=settings.API_V1_PREFIX, tags=["profiles"])
app.include_router(outcomes_router, prefix=settings.API_V1_PREFIX, tags=["outcomes"])
app.include_router(corrections_router, prefix=settings.API_V1_PREFIX, tags=["corrections"])
app.include_router(quality_router, prefix=settings.API_V1_PREFIX, tags=["quality"])
app.include_router(ws_tickets_router, prefix=settings.API_V1_PREFIX, tags=["ws-tickets"])
app.include_router(live_sessions_router, prefix=settings.API_V1_PREFIX, tags=["live-sessions"])
app.include_router(coaching_router, prefix=settings.API_V1_PREFIX, tags=["coaching"])
app.include_router(oauth_router, prefix=settings.API_V1_PREFIX, tags=["oauth"])
app.include_router(conversations_router, prefix=settings.API_V1_PREFIX, tags=["conversations"])
app.include_router(email_push_router, prefix=settings.API_V1_PREFIX, tags=["email-push"])
app.include_router(email_backfill_router, prefix=settings.API_V1_PREFIX, tags=["email-backfill"])
app.include_router(feedback_router, prefix=settings.API_V1_PREFIX, tags=["feedback"])
app.include_router(evaluation_router, prefix=settings.API_V1_PREFIX, tags=["evaluation"])
app.include_router(experiments_router, prefix=settings.API_V1_PREFIX, tags=["experiments"])
app.include_router(campaigns_router, prefix=settings.API_V1_PREFIX, tags=["campaigns"])
app.include_router(outreach_router, prefix=settings.API_V1_PREFIX, tags=["outreach"])
# Public click-tracking redirect — root-level (/t/{token}), NOT under
# /api/v1: the wrapped links live inside outreach emails and stay short.
# Routes registered here win over the catch-all static mount below.
app.include_router(outreach_links_router, tags=["outreach-links"])

# Every /admin/* endpoint requires an admin principal.
app.include_router(
    admin_router,
    prefix=settings.API_V1_PREFIX,
    tags=["admin"],
    dependencies=[_Depends(_require_role("admin"))],
)
# Motion-assignment admin endpoints (CSV import, tenant default motion).
# Routes carry their own ``require_role("admin")`` dependency so we don't
# stack two role checks.
app.include_router(
    admin_motions_router, prefix=settings.API_V1_PREFIX, tags=["admin-motions"]
)
# Authenticated support endpoints (require IT-support agent or manager).
app.include_router(support_router, prefix=settings.API_V1_PREFIX, tags=["support"])
# Public CSAT survey routes — unauthenticated, token-gated. Mounted at
# the v1 prefix so the URL the customer follows reads as the rest of the
# API surface; the route itself sits at /csat/<token> within that prefix.
app.include_router(support_csat_router, prefix=settings.API_V1_PREFIX, tags=["csat"])
# Customer Success (account health, renewals, drill-down).
app.include_router(cs_router, prefix=settings.API_V1_PREFIX, tags=["cs"])
# KB-article-request inbox + lifecycle.
app.include_router(
    kb_requests_router, prefix=settings.API_V1_PREFIX, tags=["kb-requests"]
)
# SSO/SCIM provisioning. Rule CRUD is admin-only; the SCIM endpoint is
# gated by a tenant API key (``users:write`` scope).
app.include_router(sso_router, prefix=settings.API_V1_PREFIX, tags=["sso"])
app.include_router(scim_router, prefix=settings.API_V1_PREFIX, tags=["scim"])
# Customer relationship memory.
app.include_router(
    customer_memory_router,
    prefix=settings.API_V1_PREFIX,
    tags=["customer-memory"],
)
app.include_router(auth_session_router, prefix=settings.API_V1_PREFIX, tags=["auth"])
app.include_router(
    crm_router,
    prefix=settings.API_V1_PREFIX,
    tags=["crm"],
    dependencies=[_Depends(_require_role("admin"))],
)
app.include_router(emails_router, prefix=settings.API_V1_PREFIX, tags=["emails"])
app.include_router(onboarding_router, prefix=settings.API_V1_PREFIX, tags=["onboarding"])
# Telephony ingress (Twilio voice webhook + Media Streams WS + outbound dial).
app.include_router(telephony_router, prefix=settings.API_V1_PREFIX, tags=["telephony"])
# Stripe: webhook unauthenticated (Stripe signature); admin `link` gated per-endpoint.
app.include_router(stripe_router, prefix=settings.API_V1_PREFIX, tags=["billing"])
app.include_router(
    webhooks_router,
    prefix=settings.API_V1_PREFIX,
    tags=["webhooks"],
    dependencies=[_Depends(_require_role("admin"))],
)
app.include_router(chat_router, prefix=settings.API_V1_PREFIX, tags=["chat"])
app.include_router(me_router, prefix=settings.API_V1_PREFIX, tags=["me"])
app.include_router(signup_router, prefix=settings.API_V1_PREFIX, tags=["signup"])

from backend.app.api.gdpr import router as gdpr_router  # noqa: E402

app.include_router(gdpr_router, prefix=settings.API_V1_PREFIX, tags=["gdpr"])

from backend.app.api.websocket import router as websocket_router  # noqa: E402

app.include_router(websocket_router, tags=["websocket"])


# === BEGIN MULTI-STREAM ROUTER REGION (do not edit other streams' lines) ===
# Each stream owns ONE line below. Append your include_router call after
# the colon on your stream's line. Do not reorder, edit other streams,
# or remove the BEGIN/END markers.
# See: /Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md
# stream-1/siprec:
from backend.app.api.siprec import siprec_router as _siprec_router  # noqa: E402

app.include_router(_siprec_router, prefix=settings.API_V1_PREFIX, tags=["siprec"])
# stream-2/uc:
from backend.app.api.uc_telephony import router as uc_telephony_router  # noqa: E402
app.include_router(uc_telephony_router, prefix=settings.API_V1_PREFIX, tags=["uc-telephony"])
# stream-3/teams:
from backend.app.api.teams_recording import router as teams_recording_router  # noqa: E402
app.include_router(teams_recording_router, prefix=settings.API_V1_PREFIX, tags=["teams-recording"])
# stream-4/audiohook:
from backend.app.api.audiohook import router as audiohook_router  # noqa: E402
app.include_router(audiohook_router, prefix=settings.API_V1_PREFIX, tags=["audiohook"])
# === END MULTI-STREAM ROUTER REGION ===


# ── Prometheus /metrics ──────────────────────────────────
from fastapi import Response  # noqa: E402

from backend.app.services import metrics as _ai_metrics  # noqa: E402


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    payload, content_type = _ai_metrics.metrics_handler()
    return Response(content=payload, media_type=content_type)


# ── Legal pages (stable, redirect-free URLs) ─────────────
# The static mount below would serve these as directory indexes, but a
# bare ``/privacy`` 307-redirects to ``/privacy/``. Google's OAuth consent
# screen advertises these exact URLs and expects a direct 200, so we pin
# explicit handlers ahead of the catch-all mount.
from fastapi.responses import FileResponse  # noqa: E402


@app.get("/privacy", include_in_schema=False)
def privacy_page() -> FileResponse:
    return FileResponse("website/privacy/index.html")


@app.get("/terms", include_in_schema=False)
def terms_page() -> FileResponse:
    return FileResponse("website/terms/index.html")


# ── Static Files (minimal demo UI) ───────────────────────
app.mount("/", StaticFiles(directory="website", html=True), name="website")
