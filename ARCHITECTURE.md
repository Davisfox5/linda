# LINDA — Architecture

LINDA is a white-label B2B SaaS platform that ingests customer-conversation audio and
text (phone, VoIP, video-conferencing recordings, uploaded transcripts, email), transcribes
it, and runs AI analysis to produce next steps, follow-ups, sentiment/churn/upsell signals,
QA scorecards, and live coaching. Sold to sales, customer-success, and support teams.

---

> ## 📌 Maintenance instruction (read me first)
>
> **This document must stay in sync with the code. If you change the architecture, update this file in the same PR.**
>
> Treat the codebase — not this doc — as the ultimate source of truth, and reconcile any drift you notice:
> - **Routers / endpoints:** `backend/app/main.py` (`app.include_router(...)` calls) and `backend/app/api/`
> - **Data model:** `backend/app/models.py` (single module; every `__tablename__` is a table)
> - **Scheduled work:** the `beat_schedule` dict in `backend/app/tasks.py`
> - **Process model & deploy:** `fly.toml` (`[processes]`), `Dockerfile`
> - **Tech stack / external services:** `requirements.txt`, `apps/app/package.json`
> - **LLM infrastructure:** `backend/app/services/model_catalog.py` (model ids) and `model_router.py` (call path)
>
> When you touch any of those, re-read the relevant section below and fix it if it no longer matches.
> A wrong architecture doc is worse than none — it sends future readers (human and AI) down dead ends.
> If you cannot verify a claim here against the code, delete or flag it rather than leave it asserted.

---

## 1. System shape

```
                    ┌─────────────────────────────────────────────┐
   Browser ───────► │  apps/app  — Next.js 15 / React 19 SPA       │
   (product users)  │  (Clerk auth, TanStack Query, Tailwind)      │
                    └───────────────┬─────────────────────────────┘
                                    │  HTTPS  /api/v1/*  +  WebSocket
                    ┌───────────────▼─────────────────────────────┐
   Prospects ─────► │  website/  — static marketing + demo site    │
   (marketing)      │  (vanilla JS, nginx; talks to the same API)  │
                    └───────────────┬─────────────────────────────┘
                                    │
                    ┌───────────────▼─────────────────────────────┐
                    │  FastAPI app  (backend.app.main:app)         │
                    │  ~56 routers under /api/v1 + WebSocket        │
                    └───┬───────────────────────────┬──────────────┘
                        │ enqueue (Redis broker)     │ read/write
                  ┌─────▼──────┐              ┌───────▼───────────────────────────┐
                  │  Celery    │              │  Postgres · Redis · Qdrant ·       │
                  │  worker +  │─────────────►│  Elasticsearch · S3 (optional)     │
                  │  beat      │              └────────────────────────────────────┘
                  └─────┬──────┘
                        │ external APIs
        Anthropic (Claude) · Deepgram (ASR) · Stripe · Clerk · telephony providers
```

## 2. Backend (`backend/app/`)

A single FastAPI application plus a Celery worker/beat, sharing the same code and models.

| Path | Role |
|---|---|
| `main.py` | App factory + `lifespan`; registers ~56 routers under `settings.API_V1_PREFIX` (`/api/v1`); `RequestContextMiddleware` + CORS; `GET /metrics` (Prometheus), health, and the WebSocket router |
| `models.py` | **All** SQLAlchemy models in one module — 89 tables. The central entity is **`Interaction`** (a call / email / uploaded transcript). |
| `tasks.py` | Celery app (`celery_app`), every background task, and the `beat_schedule` |
| `api/` | One module per router group (`interactions.py`, `manager.py`, `knowledge_base.py`, `scorecards.py`, `action_plans.py`, `telephony.py`, `sso_provisioning.py`, `scim.py`, `stripe_webhook.py`, `siprec.py`, `uc_telephony.py`, `teams_recording.py`, `audiohook.py`, `websocket.py`, …) |
| `services/` | ~100 service modules + subpackages (`action_plan/`, `audio/`, `crm/`, `email/`, `email_ingest/`, `kb/`, `meeting_scheduler/`, `telephony/`, `teams_recording/`). Business logic lives here; routers stay thin. |
| `auth.py` | API-key + per-user auth, tenant scoping, scope checks |
| `rls.py`, `tenant_ctx.py` | **Tenant isolation backstop.** Postgres row-level security on every tenant-scoped table (fail closed: no tenant GUC → zero rows). `rls.py` owns the table classification + policy DDL (shared by migrations and tests); `tenant_ctx.py` re-arms `app.current_tenant` on every transaction from a ContextVar set by auth / Celery task binding / webhook handlers. Runtime engines connect as the non-owner `linda_app` role via `APP_DATABASE_URL`; the owner DSN (`DATABASE_URL`) is the bypass path for Alembic/admin. New table? Follow the checklist in `tests/test_rls_scoping_guard.py`. |
| `config.py` | Pydantic-settings `Settings`; all env-driven config |
| `db.py` | SQLAlchemy engine/session setup (async for the API, sync engine for Celery tasks) |
| `observability.py`, `logging_setup.py` | Sentry (optional) + structured logging |
| `plans.py` | Subscription tiers (canonical: **Sandbox / Starter / Growth / Enterprise**) and Stripe mapping |

### 2.1 Data model highlights

`Interaction` is the hub. Around it: `Tenant`, `User`, `Customer`, `Contact`, `SupportCase`,
`InteractionScore` / `InteractionSnippet` / `InteractionComment`, knowledge base
(`KBDocument` / `KBChunk`), CRM (`CrmDealRecord` / `CrmSyncLog`), and the action model.

> **Cold outreach (out_001, 2026-07).** Prospects ARE `Customer` rows: a non-NULL
> `Customer.pipeline_status` (new → queued → contacted → replied → demo → won/lost,
> plus do_not_contact + a sticky `do_not_contact` flag) marks an account as
> outreach-managed; import/campaign metadata lives under `Customer.metadata['outreach']`.
> `Campaign` is discriminated by `kind`: the original `external` (passive ESP
> monitoring) and `outreach` — LINDA-originated 1:1 cold email sent through the
> tenant's connected Gmail/Outlook OAuth. Per-(campaign, prospect) sequence state
> (draft → approval → queued → in_sequence → replied/bounced/opted_out/completed)
> lives in `OutreachMember`; each delivered touch writes an `EmailSend` (audit +
> daily-throttle counters), a `CampaignRecipient` (RFC-822 Message-ID for reply
> attribution), and an outbound `Interaction` on the prospect. The email-ingest
> reply matcher (`services/email_ingest/ingest.py` + `services/outreach/replies.py`)
> halts sequences on reply, honors stop-keyword opt-outs (CAN-SPAM), and detects
> DSN bounces. Engine: `services/outreach/`; API: `api/outreach.py` (/prospects,
> /outreach/campaigns); sending driven by the `outreach_scheduler_tick` beat task
> (10 min) under per-campaign daily limits + a tenant-wide cap (config.py
> `OUTREACH_*`). Webhook events: `outreach.email.*`, `prospect.status_changed`,
> `campaign.completed` — see docs/webhooks.md. Campaign analytics (rollup, member
> funnel, quota) live in `services/campaign_stats.py`, shared by the REST routers,
> the Ask LINDA chat tools (`list_campaigns` / `get_campaign_stats` /
> `list_campaign_replies` in `services/linda_agent.py`), and — as sync twins — the
> hourly campaign-health monitor (`services/campaign_monitor.py`, see §2.2).

> **Action model: the DAG is canonical (4b cutover, 2026-07).** The analysis pipeline writes
> only `ActionPlan` → `ActionStep` (`action_plans` / `action_steps`, plus `StepArtifact` /
> `StepResponse`); the raw LLM suggestions also land in `Interaction.insights['action_items']`.
> `ActionItem` (`action_items`) remains **only** for manually created tasks (POST
> /action-items, Linda chat proposals, manager triage) — the pipeline no longer dual-writes it.

### 2.2 Async pipeline & scheduled work

Ingest → transcription (Deepgram, or self-hosted Whisper) → AI analysis (Claude) →
entity resolution → signal/scorecard/snippet generation → notifications/webhooks. The heavy
steps run as Celery tasks off a Redis broker, across three queues: **`priority`, `default`,
`batch`**.

**Exactly-once effects:** every paid / non-idempotent pipeline step (transcription,
segmentation, analysis, scorecards, entity resolution, plan synthesis) claims a row in
the **`interaction_step_runs` ledger** (`services/pipeline_ledger.py`) before running —
atomic claim, lease TTL, per-(interaction, step, input-hash) idempotency — so retries and
duplicate deliveries *resume* instead of re-paying LLM calls. Outputs commit in the same
transaction that marks the step succeeded ("persist-after-pay"). An hourly
`reconcile_orphan_interactions` beat task re-runs failed entity resolutions. Design and
rationale: [docs/complexity/01-pipeline-exactly-once.md](docs/complexity/01-pipeline-exactly-once.md).

`beat_schedule` (in `tasks.py`) drives ~40 recurring jobs — e.g. weekly tenant insights,
daily/weekly orchestration, outcomes backfill, calibration / IRT / churn-model fits, audio
& event retention sweeps, email-ingest polling, feedback-stream consumption, CRM sync,
WER computation, A/B variant winner selection, and the hourly campaign-health monitor
(`campaign_monitor_scan_all_tenants` → `services/campaign_monitor.py`: five deterministic
detectors over active campaigns — bounce/opt-out spikes, no engagement, stalled outreach,
quota starvation — plus a one-shot completion wrap-up written to
`Campaign.insights['completion_report']`; fires fingerprint-deduped `ManagerAlert` rows
with Haiku-rendered suggestions through the manager-alert fanout). **The dict is the
source of truth for what runs and when.**

## 3. Frontend

- **`apps/app/`** — the product SPA. **Next.js 15 + React 19**, Clerk for auth
  (`@clerk/nextjs`), TanStack Query for server state, Tailwind. Route groups under
  `src/app/(app)` (authed product) and `src/app/(auth)`; feature components under
  `src/components/`.
- **`website/`** — the static marketing site **and** an interactive demo, built with vanilla
  JS modules (`website/js/*`) served by nginx. It calls the same `/api/v1` backend and stores
  its API token in the `linda-api-key` localStorage key (a one-time migration in
  `demo.js` / `main.js` bridges any old `callsight-*` keys for returning visitors).

## 4. Data stores & external services

| System | Used for |
|---|---|
| **Postgres** | Primary datastore (SQLAlchemy 2.0, migrations via Alembic in `backend/alembic/`). Row-level security enforces tenant isolation on every tenant-scoped table — see `backend/app/rls.py` and §2. |
| **Redis** | Celery broker, pub/sub, sessions, rate limiting |
| **Qdrant** | Knowledge-base RAG vectors |
| **Elasticsearch** | Full-text transcript search |
| **S3** (optional) | Audio storage; falls back to a local dir (`$AUDIO_LOCAL_DIR`) when boto3/creds absent |
| **Anthropic** | Claude (Haiku / Sonnet / Opus) for analysis, triage, coaching, judging — see §5 |
| **Deepgram** | Batch + streaming ASR and diarization |
| **Presidio + spaCy** | PII detection / redaction |
| **Stripe** | Billing / subscriptions / webhooks |
| **Clerk** | Frontend authentication |
| **Telephony** | SIPREC, UC providers (Zoom/Teams/Webex/RingCentral), Twilio/Telnyx, Genesys AudioHook |

## 5. LLM infrastructure

All runtime Claude usage flows through four modules in `backend/app/services/`.
The governing rules (from `CLAUDE.md`): runtime uses **Haiku / Sonnet / Opus only** —
Fable (Mythos-class) is never called from app code — and no `claude-*` model id may
be hardcoded outside the catalog (`tests/test_model_catalog.py` fails the build if one is).

| Module | Role |
|---|---|
| `model_catalog.py` | **Single source of truth for model ids.** Resolves tier names (`haiku` / `sonnet` / `opus`) to ids from env-overridable settings, so a version bump or deprecated-model swap is a one-line change. Also owns per-model capability sets — ids that reject sampling params (Opus 4.7+, Sonnet 5, Fable) and ids that run adaptive thinking unless explicitly disabled (Sonnet 5) — and the failover map, which degrades **down** a tier (Opus→Sonnet→Haiku), never up. |
| `model_router.py` | **Every runtime LLM call goes through `ModelRouter`** (`ainvoke` for live endpoints, `invoke` for Celery, `astream`, `run_batch`). Selects the tier from `task_type` / complexity / transcript size unless the caller pins `forced_tier`; Opus is reserved for orchestrator-level work over aggregated summaries and never sees raw transcripts in the live path. Applies prompt caching (`cache_control: ephemeral`) to system prompts and tenant-scoped blocks, and routes non-interactive work (rollups, weekly reflection, backfill) through the Anthropic Messages Batches API (~50% discount) with per-entry failover on retryable errors. |
| `llm_client.py` | Anthropic client construction plus `acreate_with_failover`: transient errors (429/5xx/timeout) retry on the **same** model; model-unavailable (deprecated/suspended/404) fails over once to the next cheaper tier. Also `compute_max_tokens`, the project-wide `max_tokens` policy — tier-aware defaults scaling with input length under a per-tier ceiling. |
| `llm_telemetry.py` | Records every completion's usage to `llm_call_telemetry`; a nightly task aggregates per (call_site, tier) into `llm_ceiling_recommendation`, and `compute_max_tokens` consults those learned ceilings before the static caps. Fire-and-forget: a telemetry failure never fails a customer call. |

**Ask LINDA outcome loop.** Every decided write proposal writes a
`linda_action_outcomes` row (`services/linda_outcomes.py`) — confirmed, cancelled
or expired — and the daily `linda_outcome_scan` beat task settles the pending ones
from deterministic downstream state (an action item closing, an outreach member
replying, a step completing). No LLM judges anything in this loop; a test asserts
the module never imports the router. `no_signal` (nothing resolved inside the
14-day horizon) is kept distinct from `failed`, and `acceptance_summary()` divides
success only by *resolved* outcomes so recency doesn't depress a rate. This is the
chat-side equivalent of the `insight_quality_scores` flywheel.

**Ask LINDA context budget.** Chat tool results pass through
`services/linda_context.py` before they enter the message history or the persisted
transcript (which is replayed every later turn inside the 40-message window, so an
unfitted result bloats the context for the rest of the conversation). Stage 1 is
deterministic — cap free-text fields, then drop trailing rows — and always runs.
Stage 2 is an optional Haiku sub-call (`call_site="linda_condense"`, own context)
that re-selects rows against the user's question when stage 1 would drop some; it
is restricted to free-text search tools, and every row it returns is verified back
against the input by id before use, so a model can neither restate a metric nor
introduce an id. Any failure falls back to the stage-1 result — condensation never
fails a tool call. Budget + off-switch: `LINDA_TOOL_RESULT_BUDGET_CHARS` /
`LINDA_CONDENSE_ENABLED` in `config.py`.

## 6. Process model & deployment

Deployed on **Fly.io**. `fly.toml` `[processes]` defines three roles off one image:

- **`api`** — `uvicorn backend.app.main:app` (2 workers, proxy-headers)
- **`worker`** — `celery … worker -Q priority,default,batch`
- **`beat`** — `celery … beat` (schedule persisted to the `/data` volume)

`Dockerfile` builds the backend image (default `CMD` runs uvicorn). The Next.js SPA
(`apps/app`) and the static `website/` deploy as their own Fly apps. CI deploys the backend
and the SPA together — keep both wired in the deploy workflow.

## 7. Observability

Custom app metrics (named `linda_*`) are defined in `backend/app/services/metrics.py`
and exposed at `GET /metrics` in Prometheus format:

- The **api** process serves `/metrics` via FastAPI on port 8000.
- The **worker** and **beat** processes don't run FastAPI, so they start a small
  prometheus_client HTTP server (also port 8000, a free port on their machines) from
  the `worker_init` / `beat_init` hooks in `tasks.py`.
- Because the api runs 2 uvicorn workers and celery runs prefork children, metrics use
  **prometheus_client multiprocess mode**: `PROMETHEUS_MULTIPROC_DIR` (set in `fly.toml`,
  created by `docker-entrypoint.sh`) collects per-process files that `/metrics` aggregates.
- The `[[metrics]]` block in `fly.toml` is what makes Fly's managed Prometheus scrape every
  machine. **Without it, none of these metrics are collected** — so if you add metrics,
  confirm that block still covers the process that emits them.

`prometheus_client` is an optional dependency: if it's absent every metric call is a no-op,
so the app still runs (handy for local dev). Sentry (optional, `observability.py`) handles
error monitoring.

---

*Historical note: this platform was previously branded **CallSight**; it is now **LINDA**.
Residual `callsight-*` identifiers only survive where they are load-bearing migration bridges
(e.g. the localStorage key rename) — treat any other occurrence as cleanup.*
