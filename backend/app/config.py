"""Application configuration — loads all secrets from .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Core ──────────────────────────────────────────────
    APP_NAME: str = "LINDA"
    DEBUG: bool = False
    # Environment name used to tag Sentry events + metric labels so
    # staging noise doesn't bleed into prod dashboards.
    ENVIRONMENT: str = "local"
    # Deploy SHA / version. Populated by CI.
    RELEASE_VERSION: str = ""
    # Sentry DSN — empty disables error monitoring (local dev default).
    SENTRY_DSN: str = ""
    API_V1_PREFIX: str = "/api/v1"
    # Empty by default. Browsers reject ``Access-Control-Allow-Origin: *``
    # paired with ``Access-Control-Allow-Credentials: true``, so a wildcard
    # default would silently break every credentialed request in any
    # deployment that forgot to set the env var. Operators MUST set
    # explicit origins (e.g. https://linda-staging-app.fly.dev) via
    # ``ALLOWED_ORIGINS=["https://..."]`` in fly secrets / .env.
    ALLOWED_ORIGINS: list[str] = []
    # Public origin of the SPA — used to build OAuth-callback redirects
    # back into the app after a successful provider connect. Falls back
    # to the first allowed origin when unset (so staging "just works").
    SPA_URL: str = ""
    # Extra origins allowed as OAuth post-connect ``return_to`` targets —
    # for external consoles that embed LINDA's OAuth and want the user
    # bounced back to THEIR app, not LINDA's SPA. The SPA_URL origin and
    # every ALLOWED_ORIGINS entry are always permitted; this adds origins
    # that don't otherwise need CORS. e.g.
    # ``OAUTH_RETURN_TO_ALLOWED_ORIGINS=["https://console.example.com"]``.
    OAUTH_RETURN_TO_ALLOWED_ORIGINS: list[str] = []

    # ── Database (Neon PostgreSQL) ────────────────────────
    DATABASE_URL: str
    # Non-owner role DSN for the runtime engines (API + Celery). Postgres
    # table owners bypass row-level security, so the app must NOT connect
    # as the owner once RLS is live — set this to the ``linda_app`` role's
    # DSN in staging/production. Falls back to DATABASE_URL (owner) when
    # unset, which disables the RLS backstop; main.py logs a loud warning
    # at startup in that case. Migrations/admin keep using DATABASE_URL.
    APP_DATABASE_URL: Optional[str] = None

    # ── Redis (ElastiCache) ──────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Auth (Clerk — transitional) ──────────────────────
    CLERK_SECRET_KEY: str = ""
    CLERK_PUBLISHABLE_KEY: str = ""

    # Just-in-time provisioning for enterprise SSO. When a Clerk-brokered
    # SSO login (Okta/Entra/Workspace via a Clerk enterprise connection)
    # presents a valid JWT for a user with no local row yet, auto-create /
    # link the User on the tenant resolved from the token's org id or email
    # domain. Off by default — an operator opts in after mapping tenants in
    # ``tenants.features_enabled['sso']`` (see docs/sso-setup.md). Without
    # this, a net-new SSO user authenticates at Clerk but is rejected here.
    SSO_JIT_PROVISIONING_ENABLED: bool = False

    # ── Session JWTs (native per-user login) ─────────────
    # HMAC secret for signing browser session tokens. Must be ≥32 chars in
    # production. In DEBUG we fall back to an ephemeral value.
    SESSION_JWT_SECRET: str = ""
    SESSION_JWT_TTL_HOURS: int = 12

    # ── AI / LLM (Anthropic) ─────────────────────────────
    ANTHROPIC_API_KEY: str

    # How often (seconds) the credit-balance circuit breaker lets ONE
    # probe call through while open. An exhausted balance is an expected
    # operational state — the breaker pauses all LLM work, probes on
    # this interval, and resumes automatically once a probe succeeds.
    # See services/llm_circuit_breaker.py.
    LLM_BREAKER_PROBE_INTERVAL_SECONDS: int = 300

    # Model ids by tier — the SINGLE source of truth for which Claude
    # version each tier resolves to (see services/model_catalog.py). Every
    # runtime touchpoint resolves its model through the catalog, so bumping
    # a version (or swapping a deprecated/suspended model) is a one-line
    # change here or an env override — never a 25-file sweep.
    #
    # Defaults are PINNED to the currently-shipping ids. We deliberately do
    # NOT auto-pull "latest": an env override is an explicit, reviewable act.
    ANTHROPIC_MODEL_HAIKU: str = "claude-haiku-4-5-20251001"
    ANTHROPIC_MODEL_SONNET: str = "claude-sonnet-5"
    ANTHROPIC_MODEL_OPUS: str = "claude-opus-4-8"

    # ── Recommendation enrichment ────────────────────────
    # When True, every customer-targeted ManagerRecommendation gets a
    # follow-up Sonnet pass that composes a situation-specific brief from
    # the account's full context. One call per new recommendation (post
    # dedup), so daily volume is small. Off = detector one-liners only.
    RECOMMENDATION_ENRICHMENT_ENABLED: bool = True

    # ── Speech-to-Text (Deepgram) ────────────────────────
    DEEPGRAM_API_KEY: str = ""
    DEFAULT_TRANSCRIPTION_ENGINE: Literal["deepgram", "whisper"] = "deepgram"

    # ── Speaker Diarization (pyannote) ───────────────────
    # Needed to download pyannote/speaker-diarization-3.1 from HuggingFace
    # on first use. Accept the model's gated licence once per account.
    HUGGINGFACE_TOKEN: str = ""
    PYANNOTE_DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"

    # ── Vector DB ────────────────────────────────────────
    # Which backend serves KB retrieval. "pgvector" is the default and requires
    # no extra infrastructure. Flip to "qdrant" after running a reindex once the
    # pgvector health signals start firing.
    VECTOR_BACKEND: Literal["pgvector", "qdrant"] = "pgvector"
    # QDRANT_URL is intentionally an empty default rather than
    # http://localhost:6333. The readiness probe at /api/v1/ready
    # treats an empty URL as "not configured" and skips the connect
    # attempt; the old localhost default caused the probe to fail on
    # every pgvector-only deployment (no qdrant in the container).
    # Set this to a real URL only when flipping VECTOR_BACKEND to "qdrant".
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""

    # ── Embeddings (Voyage AI) ───────────────────────────
    VOYAGE_API_KEY: str = ""
    VOYAGE_EMBED_MODEL: str = "voyage-3"
    # Must match the model's output dim (voyage-3 = 1024, voyage-3-large = 2048)
    VOYAGE_EMBED_DIM: int = 1024
    KB_CHUNK_TOKENS: int = 500
    KB_CHUNK_OVERLAP_TOKENS: int = 80

    # ── Vector health monitoring ─────────────────────────
    # Thresholds for the dev-only /admin/vector-health signal.
    VECTOR_HEALTH_P95_MS: int = 150
    VECTOR_HEALTH_SIZE_MILESTONES: list[int] = [1_000_000, 3_000_000, 5_000_000]
    VECTOR_HEALTH_ALERT_DAYS: int = 7
    # Optional: when set, auto-create a GitHub issue on sustained threshold breach.
    GITHUB_ALERT_REPO: str = ""  # e.g., "davisfox5/test-5"
    GITHUB_ALERT_TOKEN: str = ""

    # ── Full-Text Search ─────────────────────────────────
    # Transcript search runs on Postgres FTS (generated ``search_vector``
    # column + GIN index; see backend/app/search_ddl.py). No external
    # search cluster — nothing to configure here.

    # ── Telephony ────────────────────────────────────────
    TELNYX_API_KEY: str = ""
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    SIGNALWIRE_PROJECT_ID: str = ""
    SIGNALWIRE_TOKEN: str = ""

    # ── Meeting Bots (Recall.ai) ─────────────────────────
    RECALL_AI_API_KEY: str = ""

    # ── OAuth — Google Workspace ─────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # ── OAuth — Microsoft ────────────────────────────────
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""

    # ── OAuth — Slack (manager-alert delivery) ───────────
    SLACK_CLIENT_ID: str = ""
    SLACK_CLIENT_SECRET: str = ""
    SLACK_SIGNING_SECRET: str = ""

    # ── CRM ──────────────────────────────────────────────
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: str = ""
    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""
    PIPEDRIVE_CLIENT_ID: str = ""
    PIPEDRIVE_CLIENT_SECRET: str = ""
    # Stub providers — config slots only; the OAuth flow is marked
    # ``certified=False`` until adapters land.
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    MICROSOFT_DYNAMICS_CLIENT_ID: str = ""
    MICROSOFT_DYNAMICS_CLIENT_SECRET: str = ""

    # ── Knowledge sources ────────────────────────────────
    # Notion public-integration OAuth app. The KB provider is fully
    # wired; the connect button stays gated until these are set.
    NOTION_CLIENT_ID: str = ""
    NOTION_CLIENT_SECRET: str = ""

    # ── Contact Enrichment ───────────────────────────────
    PDL_API_KEY: str = ""
    APOLLO_API_KEY: str = ""

    # ── Stripe (billing) ─────────────────────────────────
    STRIPE_API_KEY: str = ""
    # ``whsec_...`` signing secret from the Stripe webhook endpoint.
    STRIPE_WEBHOOK_SECRET: str = ""
    # Optional second signing secret accepted in parallel with
    # ``STRIPE_WEBHOOK_SECRET`` while a rotation is in flight. See
    # ``services/stripe_billing.verify_stripe_signature_with_rotation``
    # for the rotation procedure.
    STRIPE_WEBHOOK_SECRET_NEXT: str = ""
    # Price IDs that map 1:1 to plans.PLANS keys. Leave blank for tiers
    # you don't sell yet. Legacy STRIPE_PRICE_{SOLO,TEAM,PRO} env vars
    # are still honored as aliases for SANDBOX/STARTER/GROWTH at lookup
    # time (see stripe_billing.price_id_to_tier).
    STRIPE_PRICE_SANDBOX: str = ""
    STRIPE_PRICE_STARTER: str = ""
    STRIPE_PRICE_GROWTH: str = ""
    STRIPE_PRICE_ENTERPRISE: str = ""
    STRIPE_PRICE_SOLO: str = ""  # legacy alias → sandbox
    STRIPE_PRICE_TEAM: str = ""  # legacy alias → starter
    STRIPE_PRICE_PRO: str = ""  # legacy alias → growth
    # JSON blob with every per-tier add-on price ID (extra scorecards,
    # additional seats, live coaching, onboarding fees). Drives the
    # ``/admin/stripe/checkout`` endpoint and the scorecard entitlement
    # check. Empty / missing / malformed → checkout disabled (503) and
    # paid-extra scorecards count as zero. Shape:
    #   {
    #     "starter": {"base": {"monthly":"price_..","annual":".."},
    #                 "addl_seat": {...}, "extra_scorecard": {...},
    #                 "onboarding": {"direct":"..","partner":".."}},
    #     "growth":  {... same shape ...},
    #     "enterprise": {... same shape ...},
    #     "starter_addons": {"live_coaching": {"monthly":"..","annual":".."}}
    #   }
    STRIPE_PRICE_CATALOG: str = ""
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_REGION: str = "us-east-1"

    # ── Encryption key for OAuth tokens at rest ──────────
    TOKEN_ENCRYPTION_KEY: str = ""  # 32-byte Fernet key

    # ── Email push notifications ──────────────────────────
    # Base URL the OAuth providers can reach to POST inbound
    # notifications.  Needs to be an https URL in production.
    PUBLIC_WEBHOOK_BASE_URL: str = ""

    # ── Outreach click tracking ───────────────────────────
    # Public base the rewritten /t/{token} links point at
    # (https://lindaai.net in prod). Empty → falls back to
    # PUBLIC_WEBHOOK_BASE_URL; if that's empty too, campaigns with
    # track_clicks on send their original links rather than dead ones.
    OUTREACH_TRACKING_BASE_URL: str = ""
    # Where GET /t/{token} bounces when the token is unknown or stale.
    OUTREACH_CLICK_FALLBACK_URL: str = "https://lindaai.net"
    # Gmail Pub/Sub topic the backend subscribes each mailbox to.
    # Format: "projects/{project}/topics/{topic}".  Must have the
    # Gmail service account granted Publisher on it.
    GMAIL_PUBSUB_TOPIC: str = ""
    # Shared secret between the backend and Google Pub/Sub push
    # subscription (passed as ?token=... on the push URL so we can
    # drop any untrusted caller fast).
    GMAIL_PUSH_TOKEN: str = ""
    # Shared "clientState" string Microsoft Graph echoes back to prove
    # the subscription owner is us.
    GRAPH_CLIENT_STATE: str = ""

    # When True, the ingest poller hits every provider regardless of
    # whether Pub/Sub / Graph push is configured globally. Intended for
    # local dev + integration tests where setting up push is onerous.
    EMAIL_POLL_FORCE_ALL: bool = False

    # ── Cold outreach (campaign sending) ──────────────────
    # Per-campaign daily send ceiling used when a campaign's config
    # doesn't set one. Low by design: 1:1 outreach from a real mailbox,
    # not bulk marketing — deliverability rides the tenant's own domain.
    OUTREACH_DEFAULT_DAILY_LIMIT: int = 25
    # Tenant-wide daily ceiling across ALL outreach campaigns. A second
    # campaign can't silently double the mailbox's outbound volume past
    # what its domain reputation can absorb.
    OUTREACH_TENANT_DAILY_SEND_CAP: int = 100
    # Max sends per campaign per scheduler tick (10-min beat) — spreads
    # the daily quota across the send window instead of bursting it all
    # the moment the window opens.
    OUTREACH_MAX_SENDS_PER_TICK: int = 5
    # House copy-quality gate (services/outreach/copy_gate.py) on
    # prospect-facing campaign sends: auto-mode drafts that fail park as
    # needs_approval instead of self-approving, and the scheduler
    # re-parks any failing draft at send time rather than sending it.
    # Emergency off-switch only — transactional mail is never gated.
    OUTREACH_COPY_GATE_ENABLED: bool = True

    # ── Governed auto-executor (action plans) ──────────────
    # Global kill switch for the auto-executor. Default OFF: the beat
    # task returns a no-op immediately, regardless of any per-tenant
    # AutoExecutionPolicy row. A step is only ever auto-dispatched when
    # BOTH this is True AND the tenant has explicitly set an auto mode
    # for that step's action_class (default absent = 'manual').
    AUTO_EXECUTION_ENABLED: bool = False
    # Conservative per-tenant cap on real/shadow dispatches per beat
    # tick, so a policy misconfiguration (or a burst of newly-ready
    # steps) can't fan out unbounded sends in one run.
    AUTO_EXECUTION_MAX_DISPATCHES_PER_TENANT: int = 5

    # ── Ask LINDA context budget ──────────────────────────
    # Per-tool-result ceiling, in characters of serialized JSON, on what
    # enters the chat's working context. Raw tool output is the main way
    # a chat context bloats: one wide search can outweigh the entire
    # conversation, and the effective context window is far smaller than
    # the advertised one (context rot). Results under budget pass through
    # untouched — this only bites the wide ones.
    LINDA_TOOL_RESULT_BUDGET_CHARS: int = 6000
    # Whether an over-budget prose result may be condensed by a Haiku
    # sub-call (question-aware selection) before falling back to
    # deterministic tail-dropping. Off => deterministic only. Numeric
    # results are never model-condensed either way; see linda_context.
    LINDA_CONDENSE_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
