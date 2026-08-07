"""SQLAlchemy ORM models — every table in the LINDA schema."""

import uuid
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ──────────────────────────────────────────────────────────
# TENANTS
# ──────────────────────────────────────────────────────────


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    branding_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    transcription_engine: Mapped[str] = mapped_column(String, default="deepgram")
    automation_level: Mapped[str] = mapped_column(String, default="approval")
    pii_redaction_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pii_redaction_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    enrichment_pdl_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enrichment_apollo_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    keyterm_boost_list: Mapped[list] = mapped_column(JSONB, default=list)
    question_keyterms: Mapped[list] = mapped_column(JSONB, default=list)
    default_language: Mapped[str] = mapped_column(String, default="en")
    translation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    features_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Per-tenant acoustic baselines used by the churn/sentiment scorers.
    # Populated by the nightly orchestrator with p50/p90 percentiles of
    # per-speaker intensity, pitch etc. across the last ~90 days of
    # interactions. Shape: ``{"customer_intensity_db_p90": float,
    # "agent_pitch_std_semitones_p50": float, …}``. Empty dict until
    # the first computation completes.
    paralinguistic_baselines: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Outcomes webhook HMAC secret verified on X-Linda-Signature.
    outcomes_hmac_secret: Mapped[Optional[str]] = mapped_column(String)
    # How many hours of call audio to retain after processing.
    audio_retention_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24")
    # Per-tenant retention thresholds for the daily event_retention_sweep.
    # NULL means "use the system default" (90d / 365d) — overriding only
    # for tenants with a contractual or compliance reason to differ.
    # Audio retention reuses the existing ``audio_retention_hours`` column
    # — keeping the unit (hours) so customers who want sub-day retention
    # still have a knob; default of 168h (7 days) is set on new tenants
    # via the migration that adds these columns.
    retention_days_webhook_deliveries: Mapped[Optional[int]] = mapped_column(Integer)
    retention_days_feedback_events: Mapped[Optional[int]] = mapped_column(Integer)
    # Seat caps (admin floor 1). Total seat_limit includes the admin(s). Set
    # by apply_tier() whenever plan_tier changes.
    seat_limit: Mapped[int] = mapped_column(Integer, default=1)
    admin_seat_limit: Mapped[int] = mapped_column(Integer, default=1)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String)
    # True while a tier downgrade has left the tenant over-headcount.
    pending_seat_reconciliation: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-tenant operating brief consumed by orchestrator + agents.
    tenant_context: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_white_label: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Per-tenant escape hatch for the sandbox-only role-preview pill. When
    # True, the principal resolver and ``POST /me/preview-role`` treat the
    # tenant like a sandbox tenant for the purposes of role-preview only —
    # so a non-sandbox tenant (typically an internal/demo enterprise
    # tenant) can flip between agent/manager/admin views without being
    # demoted to the sandbox tier and losing its feature set. Defaults
    # False; sandbox tenants honour the pill regardless of this flag.
    role_preview_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Per-tenant default domain for the Action Plan synthesizer. Drives
    # which domain template (sales / customer_service / it_support /
    # generic) governs candidate generation, tone, and customer-endpoint
    # archetype. Per-user override via ``users.default_domain``; per-call
    # override via triage when its domain_prediction confidence >= 0.8.
    # Vocabulary pinned by CHECK constraint.
    default_domain: Mapped[str] = mapped_column(
        String, nullable=False, default="generic", server_default="generic"
    )
    # ── Plan + trial (Tier 1/2/3 customer-facing) ──────────
    # plan_tier: sandbox | starter | growth | enterprise
    plan_tier: Mapped[str] = mapped_column(String, nullable=False, default="sandbox", server_default="sandbox")
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Lifecycle marker driven by Stripe webhooks + the trial-expiry sweep.
    # Values: "active" (default — paying or healthy trial), "expired"
    # (trial ended), "past_due" (Stripe payment failure cooldown). The
    # require_active_subscription dependency is the load-bearing gate;
    # this column drives banners + reporting.
    subscription_status: Mapped[str] = mapped_column(
        String, default="active", server_default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    users: Mapped[List["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    api_keys: Mapped[List["ApiKey"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


# ──────────────────────────────────────────────────────────
# USERS
# ──────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Pin ``role`` to the canonical vocabulary (or NULL = legacy row,
        # treated as "agent" by the auth resolver). A free-form String
        # column previously let an early-draft 'executive' value slip in,
        # which silently failed every role-rank lookup and confused three
        # different SPA surfaces. Migration u8c9d0e1f2a3 promotes any
        # rogue value to 'admin' and installs this constraint to fail
        # loud on future drift.
        CheckConstraint(
            "role IS NULL OR role IN ('agent', 'manager', 'admin')",
            name="ck_users_role",
        ),
        # Pin ``preview_role`` to the same role-name vocabulary as
        # ``role`` (or NULL = no preview). Defense-in-depth alongside
        # the Pydantic ``Literal`` on ``POST /me/preview-role`` — a
        # raw SQL UPDATE that tries to write 'owner' will be rejected.
        CheckConstraint(
            "preview_role IS NULL OR preview_role IN ('agent', 'manager', 'admin')",
            name="ck_users_preview_role",
        ),
        # Per-user Action Plan domain override; NULL means inherit tenant
        # default. Vocabulary matches ``tenants.default_domain``.
        CheckConstraint(
            "default_domain IS NULL OR default_domain IN "
            "('sales', 'customer_service', 'it_support', 'generic')",
            name="ck_users_default_domain",
        ),
    )

    # ── Domain scopes (added in dom_001) ────────────────────────────────
    #
    # ``agent_domains`` — domains the user works front-line in. A pure
    # CS rep is ``["customer_service"]``; a blended IT/CS player is
    # ``["customer_service", "it_support"]``; a dedicated manager who
    # takes no calls is ``[]``.
    #
    # ``manager_domains`` — domains the user has manager-level visibility
    # into. Composable: a Head of Revenue might hold
    # ``["sales", "customer_service"]``; a founder running everything
    # holds all three (Journey view unlocks at 2+).
    #
    # ``is_tenant_admin`` — Settings/Admin gate. Orthogonal to manager
    # scope: a dedicated Sales Manager has ``manager_domains=["sales"]``
    # but ``is_tenant_admin=False``; the founder has both.
    #
    # The legacy ``role`` column stays as a backward-compat shim so the
    # existing ``require_role("manager")`` gates keep working. Backfilled
    # by migration ``dom_001`` from the (role, tenant.default_domain)
    # tuple to preserve current behaviour bit-for-bit.

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    clerk_user_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String)
    # agent | manager | admin. Admins can manage users, tenant settings,
    # integrations, webhooks; managers can monitor calls + approve most
    # things agents can't; agents are the call-handling role.
    role: Mapped[str] = mapped_column(String, default="agent")
    # Render-time role override for demo / internal-evaluation use. NULL
    # means "no preview, use the real role". Honored by the principal
    # resolver only when the tenant is on the sandbox tier *or* has
    # ``tenants.role_preview_enabled`` flipped on — see the two-layer
    # gate in :mod:`backend.app.auth`. The DB-level CHECK constraint
    # pins the vocabulary to ``{agent, manager, admin}``; this column
    # never relaxes ``users.role`` for security purposes.
    preview_role: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # bcrypt hash (60 chars). Null for Clerk-JWT accounts or pre-password users.
    password_hash: Mapped[Optional[str]] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When non-NULL, the user was suspended for a system reason (e.g. tier_downgrade).
    suspension_reason: Mapped[Optional[str]] = mapped_column(String)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Per-user override of ``tenants.default_domain`` for Action Plan
    # synthesis. NULL = inherit. We picked per-user over a Team table
    # because a Team model would carry a lot of incidental scope for
    # the single concern this addresses; if Teams ever land, the column
    # moves to teams cleanly.
    default_domain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Domains the user works front-line in. See module-level comment
    # above the User table for the model. Values from the canonical
    # vocabulary (``sales`` / ``customer_service`` / ``it_support`` /
    # ``generic``); validated at the API edge, not by a DB CHECK
    # (Postgres CHECK on JSONB-array contents would require a function).
    agent_domains: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Domains the user has manager visibility into. Composable; ``[]``
    # means the user is not a manager anywhere.
    manager_domains: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Tenant-level Settings/Admin gate. Orthogonal to manager scope —
    # see the comment above the User table.
    is_tenant_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="users")


# ──────────────────────────────────────────────────────────
# API KEYS
# ──────────────────────────────────────────────────────────


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String)
    # Granular scopes (e.g. ``["interactions:read", "action_items:write"]``).
    # Closed-by-default: an empty list grants no write access. ``["*"]``
    # grants every scope and is preserved as the explicit "all access"
    # opt-in. The canonical scope namespace lives on
    # ``backend.app.auth.API_KEY_SCOPES``; the ``require_scope`` factory
    # enforces it on every mutating route.
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Soft-delete: ``DELETE /api-keys/{id}`` sets this so the audit trail
    # of "this key existed and was revoked at X" survives. Authentication
    # treats any row with revoked_at != NULL as invalid.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


# ──────────────────────────────────────────────────────────
# WEBHOOKS (outbound to MSP systems)
# ──────────────────────────────────────────────────────────


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String, nullable=False)
    events: Mapped[list] = mapped_column(JSONB, default=lambda: ["*"])
    secret: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Counters maintained by the dispatcher so the admin UI can show health
    # without scanning deliveries every page load.
    last_delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookDelivery(Base):
    """One row per delivery attempt sequence. Used for retry tracking + audit.

    A single event creates one WebhookDelivery per matching webhook. The row
    gets updated in place as retries fire; the attempts list holds the per-
    retry metadata (status_code, error, timestamp).
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhooks.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    event: Mapped[str] = mapped_column(String, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # pending | sent | failed | dead_letter
    status: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[list] = mapped_column(JSONB, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# CUSTOMERS (tenants' own customers — CRM-style accounts)
# ──────────────────────────────────────────────────────────


class Customer(Base):
    """A customer of the tenant — i.e., a CRM-style account the tenant sells
    to or supports. The tenant itself is ``Tenant``; this model represents
    the *end customers* whose contacts appear on calls."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String)
    crm_id: Mapped[Optional[str]] = mapped_column(String)
    industry: Mapped[Optional[str]] = mapped_column(String)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    # Per-customer dossier, built by CustomerBriefBuilder. Readable by
    # LINDA's agents at call time to ground live coaching in what we know
    # about this specific customer.
    customer_brief: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Self-FK for enterprise hierarchy (Acme parent → Acme Logistics +
    # Acme Cloud subsidiaries). No editing UI in v1; populated from CRM
    # sync when the source CRM exposes a parent relationship.
    parent_customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL")
    )
    # IANA timezone string (e.g. "America/New_York"). Auto-default from
    # the CRM-synced HQ address or a known contact's email-signature TZ;
    # editable. Drives meeting-scheduling features later.
    timezone: Mapped[Optional[str]] = mapped_column(String)
    # Denormalized result of the nightly "strongest connection" job:
    # the Linda user with the most call airtime + email volume on this
    # customer's interactions over the trailing 90 days. NULL until the
    # first job run produces a signal.
    strongest_connection_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # ── CS columns (added in migration ``dom_003``) ─────────────────────
    # Drives the CS portal's "Upcoming renewals" strip and the
    # renewal-risk composite score. NULL until populated via CRM sync
    # or manual entry — the migration never fabricates a value.
    renewal_date: Mapped[Optional[date]] = mapped_column(Date)
    # Composite account-health score, 0-100. Computed nightly by the
    # ``account_health_job`` task. NULL means not yet computed.
    health_score: Mapped[Optional[float]] = mapped_column(Float)
    # Stage on the onboarding ladder. Vocabulary pinned by CHECK:
    # not_started | in_progress | stalled | completed. ``stalled`` is
    # distinguished from ``in_progress`` so a CS-side detector can fire
    # on stalled onboardings without paging on every account mid-progress.
    onboarding_status: Mapped[Optional[str]] = mapped_column(String(32))
    # ── Cold-outreach pipeline columns (migration ``out_001``) ──────────
    # NULL means the account is not outreach-managed (CRM-synced clients,
    # inbound leads). Set by POST /prospects/import and advanced by
    # campaign events / manual PATCH. Vocabulary pinned by CHECK below;
    # ``do_not_contact`` is a separate flag (not just a status) so DNC
    # survives later status writes — every send path re-checks the flag.
    pipeline_status: Mapped[Optional[str]] = mapped_column(String(16))
    pipeline_status_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    do_not_contact: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    __table_args__ = (
        Index("ix_customer_tenant_id", "tenant_id"),
        Index("ix_customer_tenant_name", "tenant_id", "name"),
        Index("ix_customers_tenant_renewal_date", "tenant_id", "renewal_date"),
        Index("ix_customers_tenant_pipeline_status", "tenant_id", "pipeline_status"),
        CheckConstraint(
            "pipeline_status IS NULL OR pipeline_status IN "
            "('new', 'queued', 'contacted', 'replied', 'demo', 'won', 'lost', "
            "'do_not_contact')",
            name="ck_customers_pipeline_status",
        ),
        CheckConstraint(
            "onboarding_status IS NULL OR onboarding_status IN "
            "('not_started', 'in_progress', 'stalled', 'completed')",
            name="ck_customers_onboarding_status",
        ),
        CheckConstraint(
            "health_score IS NULL OR (health_score >= 0 AND health_score <= 100)",
            name="ck_customers_health_score_range",
        ),
    )


class CustomerOwner(Base):
    """Many-to-many: which Linda users own a Customer.

    One ``primary`` per customer; secondary owners accumulate as different
    reps appear on subsequent calls. Distinct from the ``agent_id`` on a
    single interaction — that's per-call attribution; this is per-account
    accountability for routing, action-item assignment, and notification
    fan-out.
    """

    __tablename__ = "customer_owners"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "user_id", name="uq_customer_owners_customer_user"
        ),
        CheckConstraint(
            "role IN ('primary', 'secondary')",
            name="ck_customer_owners_role",
        ),
        CheckConstraint(
            "assigned_via IN ('first_uploader', 'speaker_tag', 'manual')",
            name="ck_customer_owners_assigned_via",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    assigned_via: Mapped[str] = mapped_column(String, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ──────────────────────────────────────────────────────────
# CUSTOMER OUTCOME EVENTS (lifecycle signals)
# ──────────────────────────────────────────────────────────


class EmailSend(Base):
    """Outbound email delivery record — audit + dedupe.

    Populated when an agent/admin sends a follow-up email via the stored
    Gmail/Outlook OAuth token. One row per attempt; failed deliveries are
    kept (``status='failed'``) so the UI can show a retry button.
    """

    __tablename__ = "email_sends"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), index=True
    )
    sender_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)  # google | microsoft
    to_address: Mapped[str] = mapped_column(String, nullable=False)
    cc_address: Mapped[Optional[str]] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Shape per attachment: {"kind": "kb"|"upload", "id": str,
    # "filename": str, "mime_type": str, "size_bytes": int}
    attachments: Mapped[list] = mapped_column(JSONB, default=list)
    # pending | sent | failed
    status: Mapped[str] = mapped_column(String, default="pending")
    provider_message_id: Mapped[Optional[str]] = mapped_column(String)
    error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # ── Cold-outreach attribution (migration ``out_001``) ──────────────
    # Set only for campaign-originated sends; NULL for follow-up sends.
    # campaign_id also backs the daily-throttle counters, so it is
    # indexed together with created_at.
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL")
    )

    __table_args__ = (
        # Backs the audit/list flows that page email sends in tenant +
        # newest-first order.
        Index("ix_email_send_tenant_created", "tenant_id", "created_at"),
        # Daily-throttle counters: sends per (tenant|campaign) per day,
        # keyed on sent_at (the scheduler's logical clock).
        Index("ix_email_send_campaign_sent", "campaign_id", "sent_at"),
    )


class CustomerOutcomeEvent(Base):
    """A lifecycle event on a Customer — the signal we learn from.

    Emitted by the AI analysis pipeline (when insights indicate churn/upsell
    triggers), by webhook sync from a CRM, or manually via the outcome API.
    The CustomerBriefBuilder reads these to populate the "what's worked, what's
    failed" section of each customer's brief, and the TenantBriefRefiner
    aggregates them across customers to learn tenant-wide patterns.
    """

    __tablename__ = "customer_outcome_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    # Enum-ish string. Supported values:
    #   became_customer | upsold | renewed | churned | satisfaction_change
    #   | escalation | advocate_signal | at_risk_flagged
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    magnitude: Mapped[Optional[float]] = mapped_column(Float)
    signal_strength: Mapped[Optional[float]] = mapped_column(Float)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(String)  # ai_inferred|agent_logged|crm_sync
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# CONTACTS
# ──────────────────────────────────────────────────────────


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        # Pin the buying-group role vocabulary at the DB level so a
        # rogue value (e.g. legacy "decision_maker") can't sneak in via
        # raw SQL or a bad migration; the enum is mirrored in the LLM
        # output schema and the SPA's chip styles.
        CheckConstraint(
            "role IS NULL OR role IN ('champion', 'economic_buyer', 'user', 'blocker', 'coach')",
            name="ck_contacts_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[Optional[str]] = mapped_column(String)
    email: Mapped[Optional[str]] = mapped_column(String)
    phone: Mapped[Optional[str]] = mapped_column(String)
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("customers.id"))
    crm_id: Mapped[Optional[str]] = mapped_column(String)
    crm_source: Mapped[Optional[str]] = mapped_column(String)
    # Buying-group role inferred from call dialogue. NULL until the
    # entity-resolution step has enough confidence to populate it.
    # ``role_confidence`` carries the LLM's most-recent confidence so
    # the SPA can render confirmed (≥0.8) vs suggested (0.6–0.8) chip
    # styling without recomputing.
    role: Mapped[Optional[str]] = mapped_column(String)
    role_confidence: Mapped[Optional[float]] = mapped_column(Float)
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sentiment_trend: Mapped[list] = mapped_column(JSONB, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    customer: Mapped[Optional["Customer"]] = relationship()


# ──────────────────────────────────────────────────────────
# SUPPORT CASES (IT Support motion's anchor object)
#
# Sales calls are transactional; CS works at the account level; Support
# has a ticket lifecycle that spans multiple interactions. A case is
# the join object: one customer issue, many interactions, until the
# case resolves and closes.
# ──────────────────────────────────────────────────────────


class SupportCase(Base):
    """IT-Support ticket — groups every interaction on one customer issue.

    Lifecycle: ``open`` → ``in_progress`` → optionally ``escalated``
    (still actively worked at a higher tier) → ``resolved`` (problem
    fixed, awaiting close window) → ``closed``. ``escalated_at`` and
    ``first_response_at`` are timestamps stamped on state transitions;
    the close-rate / TTR / FCR detectors read them directly.
    """

    __tablename__ = "support_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL")
    )
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # Free-form category tag — agent-set or inferred from KB taxonomy.
    category: Mapped[Optional[str]] = mapped_column(String(64))
    # open | in_progress | escalated | resolved | closed (CHECK in migration).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    # high | medium | low (CHECK in migration). Medium is the safe default.
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    # First-contact-resolution: True iff resolved within the first
    # interaction. Stamped at resolve time.
    first_contact_resolution: Mapped[Optional[bool]] = mapped_column(Boolean)
    # 1-5 (CHECK in migration). NULL when not collected yet. Feeds the
    # support CSAT-drop detector.
    csat_score: Mapped[Optional[int]] = mapped_column(Integer)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    # Subject-embedding columns for the cross-customer trend detector
    # (PR ``ai-cross-customer-trends`` / migration ``dom_008``). Stored
    # as a JSONB list of floats (Voyage 1024-dim by default) so the
    # cluster job runs deterministically in Python without a pgvector
    # index. ``embedded_at`` lets the embedder backfill stale rows.
    subject_embedding: Mapped[Optional[list]] = mapped_column(JSONB)
    embedded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    customer: Mapped[Optional["Customer"]] = relationship()
    assignee: Mapped[Optional["User"]] = relationship(foreign_keys=[assigned_to])


# ──────────────────────────────────────────────────────────
# INTERACTIONS (omnichannel — voice, email, transcript)
#
# Old SMS / WhatsApp rows from prior backfills remain readable (the
# ``channel`` column is a free-form string), but the API only accepts
# voice / email / transcript. See ``backend/app/api/interactions.py`` for
# the create-side rejection. ``transcript`` is the uploaded-text path
# (caller pastes a call transcript); legacy callers using ``"chat"`` are
# remapped to ``"transcript"`` by the API.
# ──────────────────────────────────────────────────────────


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    # Direct FK to the resolved customer. Populated by the entity-
    # resolution step in ``_run_pipeline_impl`` even when no specific
    # contact is identified (cold outbound, multi-party calls, calls
    # where the org name is inferred but no individual is named).
    # ``customer_id`` and ``contact_id`` are independent — the resolver
    # may set one, both, or neither; downstream code should not assume
    # ``contact.customer_id`` matches.
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )

    # Type and source — voice|email|transcript. The API rejects any other
    # value with a 400 (see backend/app/api/interactions.py).
    channel: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String)
    direction: Mapped[Optional[str]] = mapped_column(String)  # inbound|outbound|internal

    # Promoted from the Action-Plan-synthesizer hint to a first-class
    # property of every interaction (migration ``dom_001``). Values:
    # ``sales`` / ``customer_service`` / ``it_support`` / ``generic``.
    # Stamped at create time from (in order): explicit API input, the
    # acting user's primary ``agent_domains`` value, ``Tenant.default_domain``.
    # The analysis service dispatches its system prompt off this column;
    # the manager portal filters its narrative / alerts / recommendations
    # by it. CHECK constraint pins the vocabulary; NULL is accepted for
    # legacy rows that pre-date the backfill.
    domain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Optional FK to the support case this interaction belongs to.
    # Populated for IT-Support motion interactions; NULL for Sales/CS.
    # A case has many interactions; one interaction belongs to at most
    # one case. ``ondelete='SET NULL'`` so case-delete doesn't cascade
    # into the (shared) interactions table.
    support_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("support_cases.id", ondelete="SET NULL"), nullable=True
    )

    # Which PromptVariant produced the most recent .insights (for A/B + rollback).
    prompt_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    # Content
    title: Mapped[Optional[str]] = mapped_column(String)
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    thread_id: Mapped[Optional[str]] = mapped_column(String)

    # Audio-specific
    # ``audio_s3_key`` holds short-lived staging for direct uploads (we
    # transcribe then delete). ``audio_url`` holds a provider-hosted URL
    # for external recording systems that push us a pointer rather than
    # bytes (MiaRec, Dubber, Teams, MetaSwitch, etc.). At most one of the
    # two is set per interaction.
    audio_s3_key: Mapped[Optional[str]] = mapped_column(String)
    audio_url: Mapped[Optional[str]] = mapped_column(String)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    caller_phone: Mapped[Optional[str]] = mapped_column(String)

    # Email-specific (populated for channel='email')
    from_address: Mapped[Optional[str]] = mapped_column(String)
    to_addresses: Mapped[list] = mapped_column(JSONB, default=list)
    cc_addresses: Mapped[list] = mapped_column(JSONB, default=list)
    bcc_addresses: Mapped[list] = mapped_column(JSONB, default=list)
    subject: Mapped[Optional[str]] = mapped_column(String)
    message_id: Mapped[Optional[str]] = mapped_column(String, unique=True)  # RFC-822
    in_reply_to: Mapped[Optional[str]] = mapped_column(String)
    references: Mapped[list] = mapped_column(JSONB, default=list)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String)  # Gmail/Graph id
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[Optional[str]] = mapped_column(String)  # sales|support|it|other
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float)
    body_html: Mapped[Optional[str]] = mapped_column(Text)  # sanitized, optional

    # Processing
    status: Mapped[str] = mapped_column(String, default="processing")
    engine: Mapped[str] = mapped_column(String, default="deepgram")
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    pii_redacted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Call metrics (computed pre-LLM)
    call_metrics: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Tiered AI analysis
    complexity_score: Mapped[Optional[float]] = mapped_column(Float)
    analysis_tier: Mapped[Optional[str]] = mapped_column(String)

    # Translation
    transcript_translated: Mapped[Optional[list]] = mapped_column(JSONB)
    detected_language: Mapped[Optional[str]] = mapped_column(String)

    participants: Mapped[list] = mapped_column(JSONB, default=list)

    # Call-level outcome. Populated either by the AI analysis pass (from
    # insights.churn_risk / action_items / dispositions) or by the agent via
    # POST /interactions/{id}/outcome. Feeds both the TenantBriefRefiner
    # (what works / doesn't) and the CustomerBriefBuilder (per-customer wins).
    outcome_type: Mapped[Optional[str]] = mapped_column(String)
    outcome_value: Mapped[Optional[float]] = mapped_column(Float)
    outcome_confidence: Mapped[Optional[float]] = mapped_column(Float)
    outcome_source: Mapped[Optional[str]] = mapped_column(String)
    outcome_notes: Mapped[Optional[str]] = mapped_column(Text)
    outcome_captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    agent: Mapped[Optional["User"]] = relationship()
    contact: Mapped[Optional["Contact"]] = relationship()
    action_items: Mapped[List["ActionItem"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")
    scores: Mapped[List["InteractionScore"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")
    snippets: Mapped[List["InteractionSnippet"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")
    comments: Mapped[List["InteractionComment"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")

    __table_args__ = (
        # Hot dashboards filter by tenant + window — composite makes the
        # range scan cheap. The single-column tenant/agent/contact
        # indexes back per-rep slices and joins from related tables.
        Index("ix_interaction_tenant_created", "tenant_id", "created_at"),
        Index("ix_interaction_tenant_id", "tenant_id"),
        Index("ix_interaction_agent_id", "agent_id"),
        Index("ix_interaction_contact_id", "contact_id"),
        # Manager-portal filters narrow by domain on top of tenant; the
        # composite makes that selective without a sequential scan once
        # CS and Support traffic shows up.
        Index("ix_interactions_tenant_domain", "tenant_id", "domain"),
        # Email dedupe (ingest_email + backfill) probes by
        # (tenant, provider_message_id) once per message — without this
        # the lookup degrades to a filter over the tenant's whole
        # interaction history. Not unique: legacy rows may already hold
        # duplicates, and NULL is the norm for non-email channels.
        Index(
            "ix_interactions_tenant_provider_message_id",
            "tenant_id",
            "provider_message_id",
        ),
        # Pin the domain vocabulary at the column level. NULL is
        # accepted for legacy rows the backfill may have missed (e.g.
        # interactions on a tenant whose ``default_domain`` was NULL,
        # which the data model technically permitted before this migration).
        CheckConstraint(
            "domain IS NULL OR domain IN ('sales', 'customer_service', 'it_support', 'generic')",
            name="ck_interactions_domain",
        ),
    )


# ──────────────────────────────────────────────────────────
# ACTION ITEMS
# ──────────────────────────────────────────────────────────


class ActionItem(Base):
    """A manually created task (POST /action-items, Linda chat proposals,
    manager triage).

    4b cutover (2026-07): the analysis pipeline no longer writes rows here
    — the ActionPlan → ActionStep DAG is the canonical action model for
    interaction follow-ups. The LLM's raw suggestions remain available in
    ``Interaction.insights['action_items']``.
    """

    __tablename__ = "action_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String)
    priority: Mapped[str] = mapped_column(String, default="medium")
    # status: 'open' | 'done' | 'dismissed'. Snooze is orthogonal via ``snoozed_until``.
    status: Mapped[str] = mapped_column(String, default="open")
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String)
    email_draft: Mapped[Optional[dict]] = mapped_column(JSONB)
    call_script: Mapped[Optional[list]] = mapped_column(JSONB)
    # next_step_type: 'meeting' | 'phone_call' | 'email' | 'document_send' |
    # 'crm_update' | 'internal_loop_in' | 'other'.
    next_step_type: Mapped[Optional[str]] = mapped_column(String(32))
    # recommended_channel: 'email' | 'phone_call' | 'meeting' | 'document_send'.
    recommended_channel: Mapped[Optional[str]] = mapped_column(String(32))
    channel_reasoning: Mapped[Optional[str]] = mapped_column(Text)
    # participants: list of {name, role, side: 'customer'|'vendor', source: ...}.
    participants: Mapped[list] = mapped_column(JSONB, default=list)
    # prep_artifacts: list of strings — what the rep should bring to the next step.
    prep_artifacts: Mapped[list] = mapped_column(JSONB, default=list)
    parent_action_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("action_items.id", ondelete="SET NULL")
    )
    implicit_signal: Mapped[Optional[str]] = mapped_column(Text)
    manually_created: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback_score: Mapped[int] = mapped_column(Integer, default=0)
    # LLM pre-suggested attachments (KB doc references). Rep reviews and
    # edits before send. Shape: [{"kb_doc_id": str, "title": str, "reason": str}].
    suggested_attachments: Mapped[list] = mapped_column(JSONB, default=list)
    # What was actually sent. Shape: [{"kb_doc_id"|"upload_id": str, "title": str,
    # "filename": str, "mime_type": str, "sent_at": iso8601}].
    attachments_sent: Mapped[list] = mapped_column(JSONB, default=list)
    automation_status: Mapped[str] = mapped_column(String, default="pending")
    dismiss_reason: Mapped[Optional[str]] = mapped_column(Text)
    snoozed_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="action_items")


# ──────────────────────────────────────────────────────────
# ACTION PLANS — the DAG-based successor to ActionItem.
#
# An Action Plan is the workflow synthesized for an interaction (or a
# manually-created plan via Linda chat). It is a directed acyclic graph
# of Action Steps that flow toward a customer-facing endpoint and,
# optionally, post-completion steps (CRM writes, internal logging).
# See backend/app/services/action_plan/ for synthesis + execution.
#
# ActionItem still exists; it is the legacy flat-list shape and is being
# phased out as consumers migrate to plans. Both coexist during cutover.
# ──────────────────────────────────────────────────────────


class ActionPlan(Base):
    __tablename__ = "action_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Plans usually attach to an interaction (one plan per interaction —
    # enforced by the unique index). Manually-created plans (Linda chat,
    # admin UI) leave this NULL.
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), unique=True, index=True
    )
    # Cached for plan list views so they don't have to join through interactions.
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    # Short goal string — "Close Apex deal", "Resolve refund + retention", etc.
    goal: Mapped[Optional[str]] = mapped_column(String)
    # Which domain template generated the plan. Vocabulary pinned by CHECK.
    domain: Mapped[str] = mapped_column(
        String, nullable=False, default="generic", server_default="generic"
    )
    # 'draft' | 'active' | 'completed' | 'abandoned'.
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    # The customer-facing endpoint step (hybrid policy: present if any
    # customer-facing step exists, else NULL — plan still has steps).
    # Not a hard FK to avoid a CASCADE cycle with action_steps.plan_id.
    customer_endpoint_step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True)
    )
    # Procedures retrieved at synthesis that drove this plan.
    # Shape: [{doc_id, chunk_id, version, title, compliance_level}].
    procedures_applied: Mapped[list] = mapped_column(JSONB, default=list)
    # CRM data snapshot at synthesis time (per-provider dict).
    external_context_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Bumped when a re-plan happens (goal edit, domain switch, etc.).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    manually_created: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    steps: Mapped[List["ActionStep"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="ActionStep.created_at",
    )

    __table_args__ = (
        CheckConstraint(
            "domain IN ('sales', 'customer_service', 'it_support', 'generic')",
            name="ck_action_plans_domain",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'abandoned')",
            name="ck_action_plans_status",
        ),
        Index("ix_action_plans_tenant_status", "tenant_id", "status"),
    )


class ActionStep(Base):
    __tablename__ = "action_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_plans.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    # ── Core content (kept close to legacy ActionItem fields) ──
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # One-sentence "what this achieves" — feeds Call C as the step intent.
    intent: Mapped[Optional[str]] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String, default="medium", server_default="medium")
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    # 'email' | 'phone_call' | 'meeting' | 'document_send' | 'research'
    # | 'system_write' | 'note'. The artifact shape itself is determined
    # by what Call C produces; this is informational meta.
    recommended_channel: Mapped[Optional[str]] = mapped_column(String(32))
    channel_reasoning: Mapped[Optional[str]] = mapped_column(Text)
    # [{name, role, side: 'customer'|'vendor', source: ...}]
    participants: Mapped[list] = mapped_column(JSONB, default=list)
    prep_artifacts: Mapped[list] = mapped_column(JSONB, default=list)
    implicit_signal: Mapped[Optional[str]] = mapped_column(Text)

    # ── State machine ──
    # 'blocked' | 'ready' | 'in_progress' | 'awaiting_response' | 'done'
    # | 'skipped' | 'deleted' | 'pending_approval'. The engine transitions
    # these. 'pending_approval' is set by the governed auto-executor
    # (services/action_plan/executor.py) when a tenant's action_class
    # policy is 'approve_then_auto' — a human approves elsewhere, at
    # which point the step goes back to 'ready' for the next tick to
    # pick up under 'auto'.
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="ready", server_default="ready"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    skipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── Graph structure ──
    # Array of step ids this step depends on. The engine derives readiness
    # from this + each upstream's state.
    depends_on: Mapped[list] = mapped_column(JSONB, default=list)
    # [{slot_key, description, required: bool, filled_by_step_id,
    #   filled_value, filled_at}] — declared at synthesis; filled at runtime.
    input_slots: Mapped[list] = mapped_column(JSONB, default=list)
    # [{slot_key, description, type}] — what this step is expected to produce.
    output_schema: Mapped[list] = mapped_column(JSONB, default=list)
    # {slot_key: value} — what was actually produced (filled by Call D
    # from inbound emails / notes, or by the agent's manual override).
    output_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    # ── KB grounding ──
    # {doc_id, chunk_id, version, snippet, compliance_level} or NULL when
    # the step is AI-suggested (no procedure backed it).
    kb_source: Mapped[Optional[dict]] = mapped_column(JSONB)
    # Denormalized from kb_source for fast filter / sort.
    # 'must' | 'should' | 'may' or NULL for AI-suggested.
    compliance_level: Mapped[Optional[str]] = mapped_column(String(8))

    # 'preparation' | 'customer_endpoint' | 'post_completion'.
    role_in_plan: Mapped[str] = mapped_column(
        String(20), default="preparation", server_default="preparation"
    )

    # ── Integration target (system_write steps) ──
    # When set, the step writes to an external system. Provider must be in
    # the tenant's connected Integration rows or the step would not have
    # been synthesized (capability gate enforced at retrieval time).
    target_integration: Mapped[Optional[str]] = mapped_column(String(32))
    integration_operation: Mapped[Optional[str]] = mapped_column(String(64))

    # ── Artifact freshness / regen scheduling ──
    artifact_version: Mapped[int] = mapped_column(Integer, default=0)
    artifact_stale: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # When set, the regen scheduler waits until this timestamp before
    # firing the next regeneration (30s debounce after the most recent
    # upstream slot fill). NULL = no pending regen.
    regen_debounce_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    feedback_score: Mapped[int] = mapped_column(Integer, default=0)
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String)
    # Free-form reason logged when the agent skips a step.
    skip_reason: Mapped[Optional[str]] = mapped_column(Text)
    # Set by the synthesizer per step. True when the step's outbound
    # action (typically an email) is expected to produce a customer
    # reply, so the engine should hold the step in ``awaiting_response``
    # after "Sent" instead of jumping straight to done. False when the
    # step is fire-and-forget (informational email, system write, etc.)
    # and Sent → done is appropriate. Default False; the synthesizer
    # sets True explicitly when the email body asks for something back.
    awaits_response: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    # Lazy artifact generation: distinguishes steps whose draft body has
    # been produced (`drafted`) from steps that are waiting on critical
    # inputs (`pending_upstream`) or are about to fire Call C
    # (`ready_to_draft`) or are blocked because an upstream step that
    # provided a critical slot got skipped or deleted (`draft_blocked`).
    # The synthesizer classifies this per step after Call B and persists
    # it; the engine flips it on upstream completion. The SPA renders a
    # different per-step UI per state so reps don't see "drafts" full
    # of unfilled placeholders. Default ``drafted`` for back-compat with
    # plans built before this column existed.
    draft_state: Mapped[str] = mapped_column(
        String(24), default="drafted", server_default="drafted"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    plan: Mapped[ActionPlan] = relationship(back_populates="steps")
    artifacts: Mapped[List["StepArtifact"]] = relationship(
        back_populates="step",
        cascade="all, delete-orphan",
        order_by="StepArtifact.version",
    )
    responses: Mapped[List["StepResponse"]] = relationship(
        back_populates="step",
        cascade="all, delete-orphan",
        order_by="StepResponse.received_at",
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('blocked', 'ready', 'in_progress', 'awaiting_response', "
            "'done', 'skipped', 'deleted', 'pending_approval')",
            name="ck_action_steps_state",
        ),
        CheckConstraint(
            "role_in_plan IN ('preparation', 'customer_endpoint', 'post_completion')",
            name="ck_action_steps_role_in_plan",
        ),
        CheckConstraint(
            "compliance_level IS NULL OR compliance_level IN ('must', 'should', 'may')",
            name="ck_action_steps_compliance_level",
        ),
        Index("ix_action_steps_plan_state", "plan_id", "state"),
        Index("ix_action_steps_tenant_assigned", "tenant_id", "assigned_to"),
        Index("ix_action_steps_regen_due", "regen_debounce_until"),
    )


class StepArtifact(Base):
    """Versioned drafts produced by Call C for a step.

    Append-only — every regeneration creates a new row. The latest version
    (max ``version`` per ``step_id``) is the active artifact. Older rows
    let the agent diff "what I had vs what the AI just regenerated."
    """

    __tablename__ = "step_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_steps.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'email' | 'script' | 'research' | 'meeting' | 'system_write_payload' | 'note'.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Discriminated payload by kind. Examples:
    #   email   -> {subject, body, cc, bcc, unfilled_slots}
    #   script  -> {opening_line, bullets, closing_line, unfilled_slots}
    #   research-> {starting_points: [{url_or_source, why}], key_questions, unfilled_slots}
    #   meeting -> {agenda, proposed_times, pre_read, unfilled_slots}
    #   system_write_payload -> {integration, operation, payload, unfilled_slots}
    #   note    -> {body, unfilled_slots}
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 'haiku' | 'sonnet' — the Anthropic tier that rendered this version,
    # for cost auditing and quality A/B analysis.
    model_tier: Mapped[Optional[str]] = mapped_column(String(16))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    step: Mapped[ActionStep] = relationship(back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint("step_id", "version", name="uq_step_artifact_version"),
        Index("ix_step_artifacts_step_version", "step_id", "version"),
    )


class StepResponse(Base):
    """Anything that fulfills (or partially fulfills) a step.

    Created when an inbound email matches a step via RFC 822 References,
    when an agent adds a manual note, or when a step is manually marked
    done. ``extracted_data`` carries the Call D extraction; the engine
    flows those values into downstream input_slots.
    """

    __tablename__ = "step_responses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_steps.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    # 'inbound_email' | 'manual_note' | 'auto_mark_done' |
    # 'outbound_email_sent' | 'auto_executed' (the governed auto-executor
    # dispatched or shadow-logged this step; see executor.py).
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # FK-by-id (not declared FK) to email_messages to keep the schemas
    # weakly coupled — the email_ingest table lives in a separate concern.
    email_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    # Outbound provider_message_id from EmailSend, set when source=
    # 'outbound_email_sent' so we can match an inbound reply later.
    outbound_message_id: Mapped[Optional[str]] = mapped_column(String)
    note_text: Mapped[Optional[str]] = mapped_column(Text)
    # Call D output — extracted slot values from the source content.
    extracted_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Per-slot reason when extraction couldn't fill a slot.
    unfilled_reasons: Mapped[dict] = mapped_column(JSONB, default=dict)
    extraction_confidence: Mapped[Optional[float]] = mapped_column(Float)
    # {slot_key: verbatim_snippet} — supports the UI "what the AI based the
    # extraction on" affordance.
    source_quotes: Mapped[dict] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Set when the agent edits extracted values after the auto-apply.
    agent_overridden: Mapped[bool] = mapped_column(Boolean, default=False)

    step: Mapped[ActionStep] = relationship(back_populates="responses")

    __table_args__ = (
        CheckConstraint(
            "source IN ('inbound_email', 'manual_note', 'auto_mark_done', "
            "'outbound_email_sent', 'auto_executed')",
            name="ck_step_responses_source",
        ),
        Index("ix_step_responses_step_received", "step_id", "received_at"),
        Index(
            "ix_step_responses_outbound_msg",
            "outbound_message_id",
        ),
    )


class StepFeedbackLog(Base):
    """Per-user record of a step edit, used to adapt future plans.

    When a rep edits a step (title rephrased, due_date adjusted,
    channel switched), we record the (before, after) snapshot scoped
    to the user who made the change. The synthesizer reads recent
    feedback for the acting user when composing a new plan and
    biases its outputs toward this user's preferred shape.

    Edits are LOCAL to the user: another rep on the same tenant
    won't see this user's preferences applied to their plans. This
    matches the principle that one rep's stylistic choices shouldn't
    silently override a teammate's.

    ``changed_keys`` is the list of fields actually modified, so the
    synthesizer can apply targeted bias (e.g. "this user always
    rewrites titles to be shorter") without re-reading the whole
    diff.
    """

    __tablename__ = "step_feedback_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_plans.id", ondelete="CASCADE")
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("action_steps.id", ondelete="CASCADE")
    )
    before: Mapped[dict] = mapped_column(JSONB, default=dict)
    after: Mapped[dict] = mapped_column(JSONB, default=dict)
    changed_keys: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_step_feedback_logs_user_created",
            "user_id", "created_at",
        ),
    )


class KBIntegrationGap(Base):
    """Procedures that reference integrations the tenant has not connected.

    Populated by the Document Orchestrator whenever it extracts a
    ``procedure`` chunk whose required_integrations include a provider
    not currently connected. Cleared (or re-evaluated) when an
    integration is added or removed. Drives the admin
    "KB-integration alignment" report.
    """

    __tablename__ = "kb_integration_gaps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_chunks.id", ondelete="CASCADE")
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE")
    )
    procedure_title: Mapped[Optional[str]] = mapped_column(String)
    required_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[Optional[str]] = mapped_column(String(64))
    # 'must' | 'should' | 'may' — copied from the procedure.
    compliance_level: Mapped[str] = mapped_column(
        String(8), default="should", server_default="should"
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "chunk_id", "required_provider", "operation",
            name="uq_kb_integration_gap",
        ),
        Index("ix_kb_integration_gaps_tenant_provider", "tenant_id", "required_provider"),
    )


class CategoryTaxonomy(Base):
    """Per-tenant canonical category set for action items.

    LLM emits free-form ``category`` strings on each action item. The
    taxonomy service normalizes against this table — known aliases get
    mapped to a canonical_name, new strings get logged as candidates and
    promote to canonical once they cross the per-tenant occurrence
    threshold. Global rows (``tenant_id IS NULL``) act as the default
    set for every new tenant.
    """

    __tablename__ = "category_taxonomy"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    canonical_name: Mapped[str] = mapped_column(String(64), nullable=False)
    aliases: Mapped[list] = mapped_column(JSONB, default=list)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    promoted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# SCORECARDS
# ──────────────────────────────────────────────────────────


class ScorecardTemplate(Base):
    __tablename__ = "scorecard_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    criteria: Mapped[list] = mapped_column(JSONB, nullable=False)
    channel_filter: Mapped[Optional[list]] = mapped_column(JSONB)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InteractionScore(Base):
    __tablename__ = "interaction_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scorecard_templates.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    total_score: Mapped[Optional[float]] = mapped_column(Float)
    criterion_scores: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="scores")


# ──────────────────────────────────────────────────────────
# SNIPPETS / CALL LIBRARY
# ──────────────────────────────────────────────────────────


class InteractionSnippet(Base):
    __tablename__ = "interaction_snippets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    snippet_type: Mapped[Optional[str]] = mapped_column(String)
    quality: Mapped[Optional[str]] = mapped_column(String)
    title: Mapped[Optional[str]] = mapped_column(String)
    description: Mapped[Optional[str]] = mapped_column(Text)
    transcript_excerpt: Mapped[list] = mapped_column(JSONB, default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    in_library: Mapped[bool] = mapped_column(Boolean, default=False)
    library_category: Mapped[Optional[str]] = mapped_column(String)
    promoted_by: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="snippets")


# ──────────────────────────────────────────────────────────
# COMMENTS / ASYNC REVIEW
# ──────────────────────────────────────────────────────────


class InteractionComment(Base):
    """Comment thread shared between interaction-level review and
    action item dialogue. Exactly one of ``interaction_id`` /
    ``action_item_id`` is required (CHECK enforced at DB level)."""

    __tablename__ = "interaction_comments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    action_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("action_items.id", ondelete="CASCADE")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    timestamp_sec: Mapped[Optional[float]] = mapped_column(Float)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="comments")


# ──────────────────────────────────────────────────────────
# LIVE SESSIONS
# ──────────────────────────────────────────────────────────


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("interactions.id"))
    source: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    # Provider-side call identifier (Twilio CallSid, SIPREC session id,
    # meeting-bot id, …) so dialers/CRMs can screen-pop the live view by
    # the id they already hold (GET /live-sessions/lookup).
    external_call_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    transcript_buffer: Mapped[list] = mapped_column(JSONB, default=list)
    coaching_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class MeetingBotJob(Base):
    """Lifecycle row for a vendor meeting bot (Zoom/Meet/Teams meetings).

    One row per bot dispatched to a meeting URL via the meeting-bot
    connector. The vendor's bot id anchors webhook correlation; the
    linked ``LiveSession`` carries the actual transcript. Vendor webhook
    handlers resolve tenant context from the bot metadata LINDA attached
    at creation (never from the inbound payload alone).
    """

    __tablename__ = "meeting_bot_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    live_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("live_sessions.id", ondelete="SET NULL")
    )
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String, default="recall")
    bot_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    meeting_url: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[Optional[str]] = mapped_column(String)  # zoom | meet | teams | unknown
    status: Mapped[str] = mapped_column(String, default="requested")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_error: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# ONBOARDING INTERVIEW (one per tenant; may be resumed)
# ──────────────────────────────────────────────────────────


class CustomerNote(Base):
    """A free-form note an agent attaches to a customer during or after a call.

    Notes are durable evidence that the CustomerBriefBuilder reads on its next
    run — they let agents capture observations LINDA might have missed in the
    transcript (e.g., "Sarah mentioned she's leaving Acme next month"). Once
    the builder has folded a note's content into the brief we mark it
    ``reviewed_at``; unreviewed notes are the fresh-evidence pile.
    """

    __tablename__ = "customer_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    author_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class CustomerWarning(Base):
    """Named, explainable risk finding on a Customer (Gong-style).

    Replaces the opaque numeric "risk score" with a finite-vocabulary
    list of warnings — single_threaded, champion_silent, competitor_
    mentioned, etc. Each row has an evidence excerpt + originating
    interaction so a click on the chip can show "Linda flagged this
    because of *this* call".

    Re-detection updates an existing row (clears ``dismissed_at``,
    bumps ``last_detected_at``) rather than spawning duplicates — see
    the ``uq_customer_warnings_customer_kind`` constraint.
    """

    __tablename__ = "customer_warnings"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('single_threaded', 'champion_silent', "
            "'competitor_mentioned', 'no_next_step', 'exec_disengaged', "
            "'pricing_unapproved', 'stalled_renewal', "
            "'negative_sentiment_trend', 'other')",
            name="ck_customer_warnings_kind",
        ),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high')",
            name="ck_customer_warnings_severity",
        ),
        UniqueConstraint(
            "customer_id", "kind", name="uq_customer_warnings_customer_kind"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    evidence_text: Mapped[Optional[str]] = mapped_column(Text)
    evidence_interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), index=True
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismissed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class Commitment(Base):
    """A promise extracted from a transcript — either side.

    Distinct from ActionItem (which is a rep-side TODO). Commitments
    track both rep ("I'll send pricing Friday") and customer-side
    ("David will loop in CTO Tuesday") promises.

    ``actor_user_id`` and ``actor_contact_id`` are mutually exclusive
    (CHECK enforced); same for the target side. ``actor_side`` records
    rep/customer/unknown so the UI can render the appropriate avatar
    even when neither User nor Contact could be matched.

    ``due_date`` is anchored at extraction time to the originating
    interaction's ``created_at`` so phrases like "by Friday" stay
    meaningful when viewed weeks later.
    """

    __tablename__ = "commitments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'done', 'overdue', 'dismissed')",
            name="ck_commitments_status",
        ),
        CheckConstraint(
            "actor_side IN ('rep', 'customer', 'unknown')",
            name="ck_commitments_actor_side",
        ),
        CheckConstraint(
            "(actor_user_id IS NULL) OR (actor_contact_id IS NULL)",
            name="ck_commitments_actor_xor",
        ),
        CheckConstraint(
            "(target_user_id IS NULL) OR (target_contact_id IS NULL)",
            name="ck_commitments_target_xor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    target_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    target_contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_excerpt: Mapped[Optional[str]] = mapped_column(Text)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    actor_side: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_via: Mapped[Optional[str]] = mapped_column(String)
    completed_evidence_interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantBriefSuggestion(Base):
    """A proposed update to a tenant's onboarding-owned brief section.

    Generated by the Infer-From-Sources agent. Stays ``pending`` until an
    admin approves or rejects it. On approve we splice it into
    ``Tenant.tenant_context`` (treating the suggestion like an onboarding
    answer); on reject it's archived for audit.
    """

    __tablename__ = "tenant_brief_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Which section of the brief this proposes changes to:
    # goals | kpis | strategies | org_structure | personal_touches
    section: Mapped[str] = mapped_column(String, nullable=False)
    # Dotted key inside the section (e.g. "personal_touches.greeting_style").
    # Empty for list-append suggestions on list-typed sections.
    path: Mapped[Optional[str]] = mapped_column(String)
    # The proposed value — can be a string, list, or dict depending on section.
    proposed_value: Mapped[Any] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    # Identifiers of the source rows (interactions, events) used so an admin
    # can click through to the evidence.
    evidence_refs: Mapped[list] = mapped_column(JSONB, default=list)
    # pending | approved | rejected | superseded
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))


class OnboardingSession(Base):
    """Persistent state for a LINDA onboarding interview.

    One row per tenant (we reuse the latest non-abandoned row so admins can
    resume). State is the full ``OnboardingInterview`` state dict.
    """

    __tablename__ = "onboarding_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    started_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    # active | completed | abandoned
    status: Mapped[str] = mapped_column(String, default="active")
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# KNOWLEDGE BASE
# ──────────────────────────────────────────────────────────


class KBDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    # NULL = tenant-wide doc (shared with everyone in the tenant).
    # Populated = personal doc owned by that agent; visible to the
    # owner + their managers/admins. Filtering happens at the API layer.
    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Customer-tagged KB (added with migration ``dom_007``). NULL means
    # the document is general — applies to every customer. A populated
    # value scopes retrieval so this document only surfaces in
    # interactions with that customer (plus all the general docs).
    # Auto-set for AI-produced artifacts; agent picks for manual
    # uploads (default NULL).
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL")
    )
    title: Mapped[Optional[str]] = mapped_column(String)
    content: Mapped[Optional[str]] = mapped_column(Text)
    content_hash: Mapped[Optional[str]] = mapped_column(String)
    source_type: Mapped[Optional[str]] = mapped_column(String)
    source_url: Mapped[Optional[str]] = mapped_column(String)
    source_external_id: Mapped[Optional[str]] = mapped_column(String)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    qdrant_point_id: Mapped[Optional[str]] = mapped_column(String)
    embedded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KBChunk(Base):
    """One embedded slice of a KB document.

    For the pgvector backend, ``embedding`` stores the vector directly. For the
    Qdrant backend, the point lives in Qdrant and this row is just metadata.
    """

    __tablename__ = "kb_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True)
    # Denormalized from ``kb_documents.customer_id`` so the retrieval
    # filter is a single-column index lookup rather than a join.
    # Backfilled / re-set on every chunk write so it stays in sync with
    # the parent doc.
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL")
    )
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[Optional[int]] = mapped_column(Integer)
    content_hash: Mapped[Optional[str]] = mapped_column(String)
    # ── Document Orchestrator output ──
    # The orchestrator pass at ingest classifies each span of a document
    # into one of these kinds and extracts structured metadata. Default
    # 'context' on rows written before the orchestrator existed.
    # Vocabulary: 'procedure' | 'policy' | 'escalation_path' | 'template'
    # | 'context' | 'faq' | 'glossary' | 'contact_directory'.
    kind: Mapped[str] = mapped_column(
        String, nullable=False, default="context", server_default="context"
    )
    # Kind-specific structured fields. For 'procedure':
    #   {triggers: [str], required_steps: [{title, description, output_slots: [...]}],
    #    required_integrations: [{provider, operation, when}],
    #    applies_when: str, compliance_level: 'must'|'should'|'may'}
    # For 'policy': {rule, scope, exceptions, compliance_level}
    # For 'escalation_path': {trigger, target_role_or_team, urgency, prerequisites}
    # For 'template': {template_name, applies_to, body, variables}
    # For 'faq': {question, answer, applies_to}
    # For 'glossary': {term, definition}
    # For 'contact_directory': {entries: [{name, role, when_to_loop_in}]}
    # Empty dict for 'context' or when orchestrator yields none.
    extracted_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # Orchestrator's confidence in the classification (0..1). Spans with
    # confidence < 0.5 land as kind='context' regardless of the model's
    # initial pick and are surfaced to an admin review queue.
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float)
    # Source span in the original document (character offsets). Lets the
    # admin UI show the orchestrator's pick in situ.
    source_span_start: Mapped[Optional[int]] = mapped_column(Integer)
    source_span_end: Mapped[Optional[int]] = mapped_column(Integer)
    # pgvector column is added by the migration (sqlalchemy doesn't ship a vector type).
    # We access it via raw SQL in PgVectorStore.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_kb_chunks_tenant_kind", "tenant_id", "kind"),
        Index("ix_kb_chunks_tenant_customer", "tenant_id", "customer_id"),
    )


class PinnedKBCard(Base):
    """An agent pin on a KB chunk for a particular contact.

    While pinned, the retrieval pipeline will still surface the chunk in search
    results but will suppress re-triggering a new card for it within a session
    or subsequent calls with the same contact.
    """

    __tablename__ = "pinned_kb_cards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"))
    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_chunks.id", ondelete="CASCADE"))
    pinned_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# OAUTH INTEGRATIONS
# ──────────────────────────────────────────────────────────


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # Nullable: tenant-wide integrations (no specific authorizing user) leave this NULL.
    # Previously the upsert fell back to tenant_id, which would FK-violate when the
    # tenant id wasn't a valid users.id.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"), nullable=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)
    access_token: Mapped[Optional[str]] = mapped_column(Text)  # Fernet (AES-128-CBC + HMAC) at rest
    refresh_token: Mapped[Optional[str]] = mapped_column(Text)  # Fernet (AES-128-CBC + HMAC) at rest
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    # Provider-specific config (Salesforce instance URL, HubSpot portal id,
    # custom property mappings, etc.). Freeform JSONB so each adapter can
    # stash what it needs without a model change.
    provider_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TenantDataOpsLog(Base):
    """Audit trail for GDPR-style data operations (export, hard-delete).

    Every run of ``export_tenant`` / ``hard_delete_tenant`` writes a
    row so a data-protection review can reconstruct who asked for
    what and when. Kept separate from the general audit log because
    its retention rules are different (we never delete these rows,
    even when the target tenant is erased).
    """

    __tablename__ = "tenant_dataops_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # We store as UUID rather than FK because the tenant may be gone
    # by the time someone audits this row.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    actor_email: Mapped[Optional[str]] = mapped_column(String)
    operation: Mapped[str] = mapped_column(String, nullable=False)  # export | delete
    status: Mapped[str] = mapped_column(String, default="running")  # running | success | failed
    reason: Mapped[Optional[str]] = mapped_column(String)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Generic audit log: one row per successful mutating operation.

    The pre-existing :class:`TenantDataOpsLog` is GDPR-specific (export,
    hard-delete) and stays untouched. ``AuditLog`` covers everything else
    — interaction edits, user role changes, API key creation, webhook
    deletes, etc. — and is the single table the admin "Audit log" UI
    queries against.

    Fields:

    * ``actor_principal`` — ``user`` / ``api_key`` / ``system``.
    * ``actor_user_id`` is null for ``api_key`` and ``system`` actors.
    * ``before`` / ``after`` are JSONB snapshots so a security review can
      diff a change without rejoining the live row.
    * ``meta`` (the column is ``metadata`` in SQL — renamed in Python
      because SQLAlchemy reserves ``metadata`` on declarative bases)
      carries the request_id / IP / user-agent.

    Indexed on ``(tenant_id, created_at desc)`` because the admin list
    query pages over rows newest-first within a single tenant.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    # user | api_key | system
    actor_principal: Mapped[str] = mapped_column(String, nullable=False)
    # Dot-namespaced action e.g. "interaction.created", "user.deactivated".
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Stored as a string so we accept both UUIDs (most rows) and
    # synthetic ids ("tenant:settings:features") without type juggling.
    resource_id: Mapped[Optional[str]] = mapped_column(String)
    before: Mapped[Optional[dict]] = mapped_column(JSONB)
    after: Mapped[Optional[dict]] = mapped_column(JSONB)
    # SQLAlchemy declarative reserves ``metadata`` — store it as ``meta``
    # in Python and expose the SQL column name via ``name="metadata"``.
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CrmSyncLog(Base):
    """One row per CRM sync run. Used by the admin UI to show sync status."""

    __tablename__ = "crm_sync_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    # running | success | partial | failed
    status: Mapped[str] = mapped_column(String, default="running")
    customers_upserted: Mapped[int] = mapped_column(Integer, default=0)
    contacts_upserted: Mapped[int] = mapped_column(Integer, default=0)
    deals_upserted: Mapped[int] = mapped_column(Integer, default=0)
    briefs_rebuilt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class CrmDealRecord(Base):
    """Persisted projection of a CRM deal (Pipedrive Deal, HubSpot Deal,
    Salesforce Opportunity). Kept narrow — the source of truth stays in
    the CRM; we cache enough to power LINDA's deal-aware coaching and
    write-back decisions without re-pulling on every request.
    """

    __tablename__ = "crm_deals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)  # pipedrive | hubspot | salesforce
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[Optional[str]] = mapped_column(String)  # open|won|lost|deleted
    amount: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[Optional[str]] = mapped_column(String)
    probability: Mapped[Optional[float]] = mapped_column(Float)
    close_date: Mapped[Optional[str]] = mapped_column(String)
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    owner_name: Mapped[Optional[str]] = mapped_column(String)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "external_id", name="uq_crm_deals_tenant_provider_external"),
    )


# ──────────────────────────────────────────────────────────
# TENANT INSIGHTS (scheduled Opus analysis)
# ──────────────────────────────────────────────────────────


class TenantInsight(Base):
    __tablename__ = "tenant_insights"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    period_start: Mapped[Optional[date]] = mapped_column(Date)
    period_end: Mapped[Optional[date]] = mapped_column(Date)
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# INTERACTION ATTACHMENTS
#
# One row per file on an email (inbound or outbound). The actual bytes
# live in S3 when AWS_S3_BUCKET is configured — we only keep metadata +
# s3_key in Postgres.
# ──────────────────────────────────────────────────────────


class InteractionAttachment(Base):
    __tablename__ = "interaction_attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    filename: Mapped[Optional[str]] = mapped_column(String)
    content_type: Mapped[Optional[str]] = mapped_column(String)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    s3_key: Mapped[Optional[str]] = mapped_column(String)
    provider_attachment_id: Mapped[Optional[str]] = mapped_column(String)
    direction: Mapped[Optional[str]] = mapped_column(String)
    inline: Mapped[bool] = mapped_column(Boolean, default=False)
    content_id: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# SCORING & ORCHESTRATOR (see docs/SCORING_ARCHITECTURE.md)
# ──────────────────────────────────────────────────────────


class InteractionFeatures(Base):
    """Canonical per-interaction feature store.

    Everything computed from the transcript — deterministic metrics,
    parsed LLM output, proxy-outcome events — lives here.  Scoring and
    orchestrator code reads only from this row, never from the raw
    ``interactions.insights`` blob.
    """

    __tablename__ = "interaction_features"

    interaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interactions.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    deterministic: Mapped[dict] = mapped_column(JSONB, default=dict)
    llm_structured: Mapped[dict] = mapped_column(JSONB, default=dict)
    embeddings_ref: Mapped[Optional[str]] = mapped_column(Text)
    proxy_outcomes: Mapped[dict] = mapped_column(JSONB, default=dict)
    scorer_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    analysis_prompt_version: Mapped[Optional[str]] = mapped_column(String(64))
    triage_prompt_version: Mapped[Optional[str]] = mapped_column(String(64))
    model_used: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InterventionEvent(Base):
    """Append-only log of rep / manager / system actions on a customer.

    Required for bias correction at training time: a customer who churns
    after we flagged them and intervened is a different signal from one
    who churns after we flagged them and did nothing. Joined against
    ``customer_outcome_events`` and ``interaction_features`` to construct
    training tuples.
    """

    __tablename__ = "intervention_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Constrained to the vocabulary in the Phase 0 migration's CHECK.
    # follow_up_sent | manager_review | escalation | rep_callback |
    # discount_offered | action_item_completed | action_item_dismissed |
    # action_item_snoozed | action_item_reopened |
    # scorecard_review_completed | other
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Notification(Base):
    """Per-user notification surface.

    Drives the in-app bell + email digest. Inserted by services on
    events the recipient should know about (action item assigned,
    new comment, reject-and-return, scorecard review, manager
    coaching prompt). Constrained ``kind`` vocabulary mirrors the
    Phase 5B-6 migration's CHECK.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # action_item_assigned | action_item_comment | action_item_returned |
    # action_item_due_soon | action_item_overdue | manager_review_completed |
    # scorecard_review_assigned | system | other
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text)
    link_url: Mapped[Optional[str]] = mapped_column(String(500))
    action_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("action_items.id", ondelete="CASCADE")
    )
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaReport(Base):
    """Per-interaction structured delta consumed by the orchestrator."""

    __tablename__ = "delta_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    delta: Mapped[dict] = mapped_column(JSONB, default=dict)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClientProfile(Base):
    __tablename__ = "client_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ManagerProfile(Base):
    __tablename__ = "manager_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    manager_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BusinessProfile(Base):
    """Tenant-level rollup profile (the 'business' is the tenant firm)."""

    __tablename__ = "business_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    business_tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScorerVersion(Base):
    """A named calibration/weight snapshot for one scorer.

    Note: ``ScorerVersion`` (this file) versions *output-side* calibration
    — the Platt / Cox / IRT weights that turn raw LLM outputs and
    deterministic features into calibrated scores.  It is distinct from
    ``PromptVariant`` below, which versions *input-side* prompts sent to
    Claude.  Both coexist: ``Interaction.prompt_variant_id`` records which
    prompt produced the insights blob; ``InteractionFeatures.scorer_versions``
    records which scoring weights applied on top.
    """

    __tablename__ = "scorer_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id"))
    scorer_name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict)
    calibration: Mapped[dict] = mapped_column(JSONB, default=dict)
    ece: Mapped[Optional[float]] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CorrectionEvent(Base):
    """Active-learning correction submitted by a user.

    Distinct from ``FeedbackEvent`` below: a ``CorrectionEvent`` is a
    targeted replacement of a specific scorer's output (e.g. user marks
    "this churn_risk is wrong — it should be low").  ``FeedbackEvent``
    is the generic implicit/explicit signal stream ingested from the
    product surfaces (thumbs, copy-edit, retry, accept).
    """

    __tablename__ = "correction_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String)
    original: Mapped[dict] = mapped_column(JSONB, default=dict)
    correction: Mapped[dict] = mapped_column(JSONB, default=dict)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# CONVERSATIONS (threading across email / voice / transcript)
# Also used by Ask Linda for chat conversations + write proposals.
# ──────────────────────────────────────────────────────────


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    channel: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String)
    thread_key: Mapped[Optional[str]] = mapped_column(String, index=True)
    classification: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    prompt_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# ASK LINDA — chat conversations, messages, write proposals
# Separate namespace from the email/voice Conversation above.
# ──────────────────────────────────────────────────────────


class LindaChatConversation(Base):
    """An Ask-Linda chat thread. Columns mirror migration a1b2c3d4e5f6 —
    this model was a bare id-only stub for a while, which broke
    ``get_or_create_conversation`` at runtime (AttributeError on
    ``tenant_id``) and hid the table from the RLS scoping guard."""

    __tablename__ = "linda_chat_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    title: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# EMAIL INGESTION CURSOR
# ──────────────────────────────────────────────────────────


class EmailSyncCursor(Base):
    __tablename__ = "email_sync_cursors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), unique=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)
    history_id: Mapped[Optional[str]] = mapped_column(String)
    delta_link: Mapped[Optional[str]] = mapped_column(Text)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Expiry of the corresponding Gmail watch / Graph subscription. NULL
    # when no push subscription has ever been registered. Used to gate
    # both renewal (re-register only inside the 24h-before-expiry window)
    # and the safety-net poll (skip integrations whose push is healthy).
    push_subscription_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


# A 'running' EmailBackfillJob whose worker hasn't stamped heartbeat_at
# within this window is presumed dead (crashed worker / lost task).
# Lives here — not in the backfill service — because both the service
# (worker takeover) and the API (supersede stale jobs) need it, and the
# API must not import the service's provider-SDK dependency chain.
EMAIL_BACKFILL_HEARTBEAT_STALE_AFTER = timedelta(minutes=15)


class EmailBackfillJob(Base):
    """One historical mailbox import ("sync last N days").

    Connecting a mailbox only syncs forward (poller + push). A backfill
    job pulls the trailing ``window_days`` of Inbox + Sent through the
    same ingest→classify→analyze pipeline. The row doubles as the job
    handle the API returns, so progress polling is a single SELECT.

    Counters: ``fetched`` = messages listed from the provider,
    ``ingested`` = new Interaction rows created, ``skipped`` =
    provider-message-id duplicates already on file. Messages the
    classifier filters as internal count toward ``fetched`` only.

    Liveness: the worker stamps ``heartbeat_at`` at every checkpoint
    commit. A ``running`` job with a stale heartbeat is treated as dead —
    a redelivered task may take it over, and the start endpoint may
    supersede it instead of returning it as in-flight.
    """

    __tablename__ = "email_backfill_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)  # google | microsoft
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued"
    )  # queued | running | done | error
    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ingested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # One live job per (tenant, provider), enforced at the schema
        # level — the endpoint's SELECT-then-INSERT guard alone loses a
        # race between two concurrent POSTs (same pattern as
        # ``uq_uc_recording_jobs_provider_call`` on UcRecordingJob).
        Index(
            "uq_email_backfill_jobs_active",
            "tenant_id",
            "provider",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )


# ──────────────────────────────────────────────────────────
# MARKETING CAMPAIGNS
# ──────────────────────────────────────────────────────────


class Campaign(Base):
    """A tracked email/push campaign.

    Two kinds share the table (discriminated by ``kind``):

    - ``external`` — the original passive shape: an ESP campaign whose
      metadata/recipients/events are pushed in via /campaigns so analysis
      can correlate exposure with outcomes. LINDA never sends these.
    - ``outreach`` — LINDA-originated cold outreach (migration ``out_001``):
      LINDA drafts, throttles, and sends 1:1 email through the tenant's
      connected Gmail/Outlook OAuth, tracks per-prospect sequence state in
      ``outreach_members``, and reuses ``campaign_recipients`` /
      ``campaign_events`` for send attribution + engagement, so the reply
      matcher in email_ingest works for both kinds unchanged.
    """

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String)
    external_id: Mapped[Optional[str]] = mapped_column(String)
    subject: Mapped[Optional[str]] = mapped_column(String)
    variant: Mapped[Optional[str]] = mapped_column(String)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # ── Outreach columns (migration ``out_001``) ────────────────────────
    kind: Mapped[str] = mapped_column(String, default="external", server_default="external")
    # external campaigns stay 'active' (harmless); outreach campaigns walk
    # draft → active ⇄ paused → completed.
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    # Outreach config — validated by services/outreach/common.py
    # ``OutreachConfig`` (template + CAN-SPAM identity, daily_limit,
    # send_window, steps, max_touches, mode). Kept as JSONB so the
    # external kind carries an empty dict and config evolves without
    # migrations; the *validated* shape is the API contract.
    config: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('external', 'outreach')", name="ck_campaigns_kind"
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'completed', 'archived')",
            name="ck_campaigns_status",
        ),
    )


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    email_address: Mapped[Optional[str]] = mapped_column(String)
    external_message_id: Mapped[Optional[str]] = mapped_column(String)
    rfc822_message_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # ── Outreach columns (migration ``out_001``) ────────────────────────
    # For outreach sends: one recipient row per delivered touch, so a
    # reply to *any* touch in the sequence attributes via its own
    # rfc822_message_id. customer_id short-circuits reply → prospect
    # resolution without joining through contacts; step is the 0-based
    # sequence-step index this row delivered.
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL")
    )
    step: Mapped[Optional[int]] = mapped_column(Integer)


class CampaignEvent(Base):
    __tablename__ = "campaign_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    recipient_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("campaign_recipients.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class OutreachMember(Base):
    """Per-(campaign, prospect) cold-outreach sequence state.

    One row per prospect enrolled in an outreach campaign. Holds the
    sequence cursor (which step is next, when it's due), the current
    step's draft + its review state, and the terminal outcome. Delivered
    touches live in ``campaign_recipients`` (one row per send, keyed by
    rfc822_message_id for reply attribution) and ``email_sends`` (audit);
    this row is only the *state machine*.

    States:
      draft_pending   — enrolled, current step has no draft yet
      needs_approval  — draft generated, waiting for human review
      queued          — approved (or auto mode), scheduler may send
      in_sequence     — ≥1 touch delivered, awaiting reply / next bump
      replied / bounced / opted_out — terminal, set by the ingest hooks
      completed       — sequence exhausted (max touches, no reply)
      failed          — provider send failed hard
      halted          — stopped manually or by prospect-level DNC
    """

    __tablename__ = "outreach_members"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "customer_id", name="uq_outreach_member_campaign_customer"
        ),
        # Scheduler scan: due members per campaign per state.
        Index("ix_outreach_members_campaign_state", "campaign_id", "state"),
        Index("ix_outreach_members_tenant_state", "tenant_id", "state"),
        CheckConstraint(
            "state IN ('draft_pending', 'needs_approval', 'queued', 'in_sequence', "
            "'replied', 'bounced', 'opted_out', 'completed', 'failed', 'halted')",
            name="ck_outreach_members_state",
        ),
        CheckConstraint(
            "draft_status IS NULL OR draft_status IN "
            "('generating', 'ready', 'approved', 'rejected')",
            name="ck_outreach_members_draft_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(String, default="draft_pending", nullable=False)
    # 0-based index of the NEXT step to send; touches_sent counts delivered.
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    touches_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_send_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    halt_reason: Mapped[Optional[str]] = mapped_column(String)
    # Current step's draft. History of what actually went out is in
    # email_sends; a bump regenerates these fields for the new step.
    draft_subject: Mapped[Optional[str]] = mapped_column(Text)
    draft_body: Mapped[Optional[str]] = mapped_column(Text)
    draft_status: Mapped[Optional[str]] = mapped_column(String)
    # Snapshot of the personalization inputs the draft was generated from
    # ({hook, segment, current_software, ...}) so a reviewer can see what
    # the model saw and regeneration is reproducible.
    personalization: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Prior touches' RFC-822 Message-IDs, oldest first — threading headers
    # for bumps (In-Reply-To/References) so the sequence is one thread in
    # the recipient's mailbox.
    thread_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OutreachLink(Base):
    """One rewritten destination inside one outreach send (``out_002``).

    When a campaign opts into click tracking (``config.track_clicks``),
    every http(s) link in the HTML part is swapped at send time for an
    opaque ``/t/{token}`` redirect. This row is the server side of that
    token: the public GET /t/{token} endpoint resolves it, records a
    ``click`` CampaignEvent, and 302s to ``original_url`` — the
    destination is never taken from the request, so the endpoint can't
    be used as an open redirect. The text/plain part keeps the original
    URLs. One row per distinct URL per send; ``recipient_id`` ties the
    click back to the exact touch that delivered it.
    """

    __tablename__ = "outreach_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # Opaque, unguessable (secrets.token_urlsafe) — the only lookup key
    # the public endpoint accepts.
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("outreach_members.id", ondelete="SET NULL")
    )
    recipient_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("campaign_recipients.id", ondelete="SET NULL")
    )
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# CONTINUOUS AI IMPROVEMENT
# ──────────────────────────────────────────────────────────


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    action_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("action_items.id"))
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    surface: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    signal_type: Mapped[str] = mapped_column(String, nullable=False)
    insight_dimension: Mapped[Optional[str]] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FeedbackDailyRollup(Base):
    """Daily per-(surface, event_type) count of feedback_events.

    Populated by the retention sweep (``event_retention.sweep_feedback_events``)
    before raw feedback_events rows older than the retention window are
    dropped. Read by ``feedback_service.feedback_volume_by_day``
    (``GET /feedback/volume``), which unions these rows with live
    feedback_events counts so volume-over-time charts keep working past
    the raw retention horizon.
    """

    __tablename__ = "feedback_daily_rollup"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    surface: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TranscriptCorrection(Base):
    __tablename__ = "transcript_corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_at_correction: Mapped[Optional[float]] = mapped_column(Float)
    corrected_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    correction_source: Mapped[str] = mapped_column(String, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InsightQualityScore(Base):
    __tablename__ = "insight_quality_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    surface: Mapped[str] = mapped_column(String, nullable=False)
    evaluator_type: Mapped[str] = mapped_column(String, nullable=False)
    evaluator_id: Mapped[str] = mapped_column(String, nullable=False)
    dimension: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    prompt_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PromptVariant(Base):
    """Versioned LLM prompt template, A/B-routable across production surfaces.

    See the note on ``ScorerVersion`` for how these two versioning systems
    relate: prompt variants govern the *input* to Claude, scorer versions
    govern the *output*-side calibration.
    """

    __tablename__ = "prompt_variants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    target_surface: Mapped[str] = mapped_column(String, nullable=False)
    target_tier: Mapped[Optional[str]] = mapped_column(String)
    target_channel: Mapped[Optional[str]] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="draft")
    parent_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class TenantPromptConfig(Base):
    __tablename__ = "tenant_prompt_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), unique=True)
    active_prompt_variant_ids: Mapped[dict] = mapped_column(JSONB, default=dict)
    few_shot_pool: Mapped[dict] = mapped_column(JSONB, default=dict)
    persona_block: Mapped[Optional[str]] = mapped_column(Text)
    acronyms: Mapped[dict] = mapped_column(JSONB, default=dict)
    custom_terms: Mapped[list] = mapped_column(JSONB, default=list)
    rag_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    parameter_overrides: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))


class VocabularyCandidate(Base):
    __tablename__ = "vocabulary_candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    term: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[str] = mapped_column(String, default="medium")
    source: Mapped[Optional[str]] = mapped_column(String)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending")
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvaluationReferenceSet(Base):
    __tablename__ = "evaluation_reference_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id"))
    surface: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    interaction_ids: Mapped[list] = mapped_column(JSONB, default=list)
    reference_outputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    surface: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="running")
    hypothesis: Mapped[Optional[str]] = mapped_column(Text)
    control_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    treatment_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    conclusion: Mapped[Optional[str]] = mapped_column(Text)
    decided_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WerMetric(Base):
    __tablename__ = "wer_metrics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    asr_engine: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[Optional[str]] = mapped_column(String)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    word_error_rate: Mapped[float] = mapped_column(Float, default=0.0)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CrossTenantAnalytic(Base):
    """Cross-tenant aggregate metric — no ``tenant_id`` column by design."""

    __tablename__ = "cross_tenant_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    metric_name: Mapped[str] = mapped_column(String, nullable=False)
    bucket: Mapped[Optional[str]] = mapped_column(String)
    surface: Mapped[Optional[str]] = mapped_column(String)
    channel: Mapped[Optional[str]] = mapped_column(String)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[Optional[float]] = mapped_column(Float)
    distribution: Mapped[dict] = mapped_column(JSONB, default=dict)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# OUTCOME EVENT INGESTION (idempotency + dead-letter)
# ──────────────────────────────────────────────────────────


class OutcomeEventIngestion(Base):
    """Idempotency record for externally-posted outcome events.

    Uniquely keyed on ``(tenant_id, event_id)`` — a repeated webhook
    delivery with the same ``event_id`` is 200-accepted but does not
    re-apply the event to ``InteractionFeatures.proxy_outcomes``.
    """

    __tablename__ = "outcome_event_ingestions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "event_id", name="uq_outcome_ingestion_event"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    outcome_type: Mapped[str] = mapped_column(String, nullable=False)
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    # First-class customer attribution (optional, validated against the
    # interaction's resolved customer at ingest) so calibration and
    # reporting can aggregate per customer without joining through
    # interactions.
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DroppedOutcomeEvent(Base):
    """Dead-letter log for outcome payloads that failed validation.

    Captured reasons include: ``unknown_outcome_type``, ``future_timestamp``,
    ``hmac_signature_invalid``, ``interaction_not_found``, ``schema_error``.
    """

    __tablename__ = "dropped_outcome_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id"))
    reason: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    headers_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LindaChatMessage(Base):
    """Every turn in a Linda chat — user message, assistant reply, or tool result."""

    __tablename__ = "linda_chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linda_chat_conversations.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String, nullable=False)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[Optional[list]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WriteProposal(Base):
    """A draft write Linda proposed. Only dispatches after explicit user confirm."""

    __tablename__ = "write_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linda_chat_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", server_default="pending")
    resulting_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# PUBLIC DEMO — email capture (pre-signup leads)
# ──────────────────────────────────────────────────────────


class DemoEmailCapture(Base):
    """Emails collected from the public demo's 60-second gate. Pre-tenant."""

    __tablename__ = "demo_email_captures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source: Mapped[Optional[str]] = mapped_column(String)
    utm: Mapped[dict] = mapped_column(JSONB, default=dict)
    converted_tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# === BEGIN MULTI-STREAM MODELS REGION ===
# Each telephony integration stream owns ONE block below. Append your model
# class definitions after your stream's header line. Do not modify
# ``LiveSession`` (line 736) or any other stream's models.
# See: /Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md
#
# stream-1/siprec:
class SiprecSession(Base):
    """One SIPREC recording session as forked from a customer's SBC.

    Sibling to :class:`LiveSession` (which we don't modify): the live
    coaching pipeline reads from ``LiveSession`` regardless of the
    ingest source, while ``SiprecSession`` carries the SIPREC-specific
    metadata that has no analogue in CPaaS sources (the SRC's call id,
    the rs-metadata XML, the negotiated crypto suite, consent
    attestation). One-to-one with ``LiveSession`` via
    ``live_session_id`` — populated by ``SiprecBridge.handle_started``
    when the SRS reports ``recording.started``.
    """

    __tablename__ = "siprec_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    live_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("live_sessions.id", ondelete="SET NULL"), index=True
    )
    integration_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("integrations.id", ondelete="SET NULL"), index=True
    )
    # One of the SIPREC TelephonyProvider Literal values
    # (siprec_cisco_cube | siprec_avaya_sbce | siprec_metaswitch).
    provider: Mapped[str] = mapped_column(String, nullable=False)
    # The recording session id from the rs-metadata XML root —
    # globally unique-ish per SBC and the natural idempotency key for
    # repeated ``recording.started`` deliveries.
    src_session_id: Mapped[str] = mapped_column(String, nullable=False)
    # SBC-side dialog identifier (Call-ID header on the recorded
    # call). Useful for correlating against the customer's CDRs.
    src_call_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    # The full rs-metadata document as parsed JSON — participants,
    # streams, communication-session refs.
    src_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # Negotiated SDES suite (e.g. ``AES_256_CM_HMAC_SHA1_80``) or
    # ``"DTLS_SRTP"`` when DTLS handled the key exchange. The master
    # key is **never** stored — we only persist the suite name so
    # ops can answer "did this customer use weak crypto?" without
    # creating a key-leak surface.
    sdp_crypto_suite: Mapped[Optional[str]] = mapped_column(String)
    # Per-tenant attestation that legal consent for recording is in
    # place. Defaults to False so the bridge fails closed in
    # jurisdictions that require explicit consent capture.
    is_consent_attested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # SRS-supplied stop reason (``hangup`` | ``timeout`` | ``error``
    # | etc.) — left as freeform text because vendors are inconsistent.
    end_reason: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("src_session_id", name="uq_siprec_sessions_src_session_id"),
    )
#
# stream-2/uc:
class UcRecordingJob(Base):
    """One row per UC vendor recording webhook delivery.

    Idempotency anchor for RingCentral / Webex Calling / Zoom Phone.
    Unique on (provider, external_call_id) so duplicate webhook
    deliveries are no-ops.
    """

    __tablename__ = "uc_recording_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False
    )
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    external_call_id: Mapped[str] = mapped_column(String, nullable=False)
    recording_id: Mapped[str] = mapped_column(String, nullable=False)
    recording_url: Mapped[Optional[str]] = mapped_column(Text)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    started_at_provider: Mapped[Optional[str]] = mapped_column(String)
    direction: Mapped[Optional[str]] = mapped_column(String)
    caller_phone: Mapped[Optional[str]] = mapped_column(String)
    callee_phone: Mapped[Optional[str]] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    state: Mapped[str] = mapped_column(
        String, default="pending", server_default="pending", nullable=False
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_error: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_call_id",
            name="uq_uc_recording_jobs_provider_call",
        ),
        CheckConstraint(
            "state IN ('pending','in_progress','fetched','dispatched','done','failed')",
            name="ck_uc_recording_jobs_state",
        ),
    )
#
# stream-3/teams:
# (TeamsCallRecord goes here)
class TeamsCallRecord(Base):
    """Microsoft Teams compliance recording control-plane row.

    One row per Teams call we have *observed* (via Graph change
    notification or — eventually — the .NET media bot's lifecycle
    events). The actual recorded media lives elsewhere; this row is the
    join key between Graph's call identifiers and LINDA's interaction
    pipeline.

    The scaffold does not write rows yet — that requires the media bot.
    The table exists so the migration is in place when the bot ships,
    and so admin tooling/queries can be wired without a follow-on
    schema change.

    Fields:

    * ``call_id`` — Microsoft's call identifier (GUID-shaped string from
      ``communications/calls`` resource).
    * ``organizer`` — UPN of the meeting organiser, or None if unknown.
    * ``participants`` — JSONB array of ``{"upn": str, "role": str,
      "joined_at": str}``. Schema is loose so the bot can stash extra
      fields without a migration.
    * ``join_url`` — Teams join URL when the call originated from a
      scheduled meeting.
    * ``recording_url`` — Graph URL for the recorded media artifact when
      one was produced (Teams built-in recording, not the media bot
      output). Nullable until ``getAllRecordings`` reports one.
    * ``certification_status`` — one of ``scaffold``, ``bot_required``,
      ``recording_fetched``. ``scaffold`` is the steady state until the
      .NET bot is deployed; ``bot_required`` indicates we observed a
      call that should have been recorded but the bot wasn't reachable;
      ``recording_fetched`` is the success terminal.
    """

    __tablename__ = "teams_call_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    call_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    organizer: Mapped[Optional[str]] = mapped_column(String)
    participants: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    join_url: Mapped[Optional[str]] = mapped_column(Text)
    recording_url: Mapped[Optional[str]] = mapped_column(Text)
    certification_status: Mapped[str] = mapped_column(
        String, default="scaffold", server_default="scaffold", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "call_id", name="uq_teams_call_records_tenant_call"),
        CheckConstraint(
            "certification_status IN ('scaffold','bot_required','recording_fetched')",
            name="ck_teams_call_records_certification_status",
        ),
    )
#
# stream-4/audiohook:
# (AudiohookSession goes here)


class AudiohookSession(Base):
    """One Genesys Cloud AudioHook conversation streamed into LINDA.

    Sibling of :class:`LiveSession` — the AudioHook flow doesn't
    create LiveSession rows because the agent attribution arrives
    inside the protocol envelope (``participant.id`` + customConfig)
    rather than from a Twilio-style outbound call setup. Linking a
    row here to a LiveSession (when the same agent has both a CPaaS
    session and an AudioHook session) is left for a follow-up.

    ``channel`` mirrors the AudioHook ``media[].channels`` semantics:
    ``"agent"`` for the internal leg only, ``"customer"`` for the
    external leg only, ``"both"`` when stereo or a mono mix carries
    both. The string is denormalized from ``provider_config`` so the
    admin UI can filter sessions without parsing JSONB.

    ``is_consent_attested`` records whether the customer has been
    consented to recording — the AudioHook integration relies on
    Genesys' built-in consent flow, but tenants can override the
    attestation per-session via a future admin UI.
    """

    __tablename__ = "audiohook_sessions"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('agent', 'customer', 'both', 'unknown')",
            name="ck_audiohook_sessions_channel",
        ),
        UniqueConstraint(
            "tenant_id",
            "audiohook_session_id",
            name="uq_audiohook_sessions_tenant_session",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    audiohook_session_id: Mapped[str] = mapped_column(String, nullable=False)
    organization_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    participant_id: Mapped[Optional[str]] = mapped_column(String)
    channel: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    media_format: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    audio_frames_received: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    audio_bytes_received: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    is_consent_attested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
# === END MULTI-STREAM MODELS REGION ===


# ──────────────────────────────────────────────────────────
# MANAGER VIEW — alerts, recommendations, channel config,
# coaching notes, Slack integration.
# ──────────────────────────────────────────────────────────


class ManagerAlert(Base):
    """Append-only feed of detected anomalies for the manager dashboard.

    One row per detected spike. ``fingerprint`` + a partial unique index
    (``WHERE resolved_at IS NULL``) means a recurring spike won't re-fire
    until the previous one is marked resolved by the auto-resolver.
    """

    __tablename__ = "manager_alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Sales: topic_spike | sentiment_drop | churn_surge | methodology_drop
    # CS:    renewal_risk_spike | health_score_drop
    # Support: csat_drop_support | escalation_surge | ttr_drift
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    # high | medium | low
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which motion this alert belongs to. Drives which tab on the
    # Manager portal renders it. NULL is accepted for legacy alerts;
    # backfilled to tenant.default_domain by migration ``dom_002``.
    domain: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    acknowledged_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismiss_reason: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# Every category any writer may stamp on a ManagerRecommendation —
# the builder's per-domain whitelists plus the cohort/trend detectors
# and the orchestrator's coaching fallbacks. The DB CHECK constraint
# (``ck_manager_recommendations_category``, migration ``sen_001``) is
# generated from this tuple, and tests/test_manager_recommendation_categories.py
# fails if a writer's category set escapes it. Adding a category here
# REQUIRES a migration recreating the constraint — the 2026-07 drift
# (constraint stuck at the 4 original sales categories while the code
# grew 13 more) made every newer-category INSERT die with
# CheckViolation and poisoned the builder's session (Sentry 2T).
MANAGER_RECOMMENDATION_CATEGORIES = (
    # sales
    "coach_rep",
    "run_campaign",
    "outreach_at_risk_customer",
    "promote_winning_script",
    "prevent_lead_stall",
    "address_sales_trend",
    # customer service
    "schedule_qbr",
    "flag_renewal_risk",
    "assign_expansion_play",
    "coach_csm",
    "prevent_no_touch_churn",
    "proactive_outreach_repeat_support",
    "address_cs_trend",
    # IT support
    "update_kb_article",
    "route_to_specialist",
    "coach_support_agent",
    "escalate_recurring_issue",
    "address_recurring_issue",
)


class ManagerRecommendation(Base):
    """Proactive next-move queue for managers. One-click apply maps each
    category to a concrete artifact (coaching note, draft campaign,
    outreach action item, or playbook entry).
    """

    __tablename__ = "manager_recommendations"
    __table_args__ = (
        CheckConstraint(
            "category IN ("
            + ", ".join("'%s'" % c for c in MANAGER_RECOMMENDATION_CATEGORIES)
            + ")",
            name="ck_manager_recommendations_category",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Sales:   coach_rep | run_campaign | outreach_at_risk_customer | promote_winning_script
    # CS:      schedule_qbr | flag_renewal_risk | assign_expansion_play | coach_csm
    # Support: update_kb_article | route_to_specialist | coach_support_agent | escalate_recurring_issue
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    # Which motion this recommendation belongs to. Backfilled to
    # tenant.default_domain by migration ``dom_002``; new rows are
    # stamped from the producing detector/builder.
    domain: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    target: Mapped[dict] = mapped_column(JSONB, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    # open | applied | dismissed | expired
    status: Mapped[str] = mapped_column(String(16), default="open")
    applied_artifact_type: Mapped[Optional[str]] = mapped_column(String(48))
    applied_artifact_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    applied_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismiss_reason: Mapped[Optional[str]] = mapped_column(Text)
    # Composed account brief written by the enrichment pass
    # (``recommendation_enrichment``). NULL until enrichment runs (or when
    # it fails / is disabled; ``rationale`` is the fallback). Shape:
    # ``{"headline": str, "sections": [{"kind", "title", "body"?, "items"?}]}``
    # where ``kind`` is one of ``recommendation_enrichment.SECTION_KINDS``.
    brief: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    enriched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AlertChannelConfig(Base):
    """Per-tenant manager-alert configuration: which channels fire at
    which severity, plus optional threshold overrides for the three
    anomaly detectors. NULL on a threshold column means "use the code
    default" — see ``anomaly_detector.DEFAULT_THRESHOLDS``.
    """

    __tablename__ = "alert_channel_config"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    inapp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    slack_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # high | medium | low — minimum severity that goes to Slack
    slack_min_severity: Mapped[str] = mapped_column(String(16), default="medium")
    topic_spike_pct_change_threshold: Mapped[Optional[int]] = mapped_column(Integer)
    topic_spike_min_volume: Mapped[Optional[int]] = mapped_column(Integer)
    sentiment_drop_threshold: Mapped[Optional[float]] = mapped_column(Float)
    churn_surge_multiplier: Mapped[Optional[float]] = mapped_column(Float)
    methodology_drop_threshold: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CustomerConcern(Base):
    """A tracked pain-point / worry / risk for one customer.

    Added in ``dom_006`` to power the customer relationship memory.
    LINDA writes to this table at analysis time so every motion
    (Sales / CS / Support) sees the same evolving picture: what's
    active now, what's calmed down to monitoring, what's been
    resolved, what's gone dormant.

    Concerns are upserted per (customer, topic); each interaction
    that touches the concern appends to ``evidence`` so the UI can
    show provenance without re-running the analyzer.

    Lifecycle: ``active`` → ``monitoring`` → ``resolved`` or
    ``dormant``. The nightly job transitions stale ``active`` rows
    to ``dormant`` after N days without a mention.
    """

    __tablename__ = "customer_concerns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium"
    )
    source_motion: Mapped[Optional[str]] = mapped_column(String(32))
    first_seen_interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    last_seen_interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'monitoring', 'resolved', 'dormant')",
            name="ck_customer_concerns_status",
        ),
        CheckConstraint(
            "severity IN ('high', 'medium', 'low')",
            name="ck_customer_concerns_severity",
        ),
        CheckConstraint(
            "source_motion IS NULL OR source_motion IN "
            "('sales', 'customer_service', 'it_support', 'generic')",
            name="ck_customer_concerns_source_motion",
        ),
        UniqueConstraint(
            "tenant_id",
            "customer_id",
            "topic",
            name="uq_customer_concerns_customer_topic",
        ),
        Index(
            "ix_customer_concerns_customer_status",
            "customer_id",
            "status",
        ),
        Index(
            "ix_customer_concerns_tenant_status",
            "tenant_id",
            "status",
        ),
    )


class CustomerCommitment(Base):
    """A commitment the CUSTOMER made to us.

    The existing ``action_items`` table tracks commitments we make
    (rep → customer). This is the symmetric mirror: what they promised.
    Powers the "they said they'd send the contract by Friday" timeline
    + a future broken-commitment detector that flags accounts whose
    promises keep slipping.
    """

    __tablename__ = "customer_commitments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    source_interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quote: Mapped[Optional[str]] = mapped_column(Text)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open"
    )
    met_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'met', 'broken', 'dismissed')",
            name="ck_customer_commitments_status",
        ),
        Index(
            "ix_customer_commitments_customer_status",
            "customer_id",
            "status",
        ),
    )


class MotionProvisioningRule(Base):
    """IDP group → motion-scope mapping.

    Added in ``dom_005`` for SSO/SCIM auto-provisioning. Tenant admin
    creates one rule per IDP group: when the user shows up via SSO or
    SCIM with that group in their claims, the rule contributes its
    ``agent_domains`` / ``manager_domains`` / ``grants_tenant_admin`` to
    the resolved scope set. Multiple rules merge; the result is the
    union across every matching rule.

    Closed-by-default: a user whose IDP claims don't match any rule
    gets nothing. The tenant default-motion is invite-time-only and
    does not apply to SSO-driven provisioning.
    """

    __tablename__ = "motion_provisioning_rule"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_domains: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    manager_domains: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    grants_tenant_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "group_name", name="uq_motion_rule_tenant_group"
        ),
        Index("ix_motion_rule_tenant_active", "tenant_id", "is_active"),
    )


class ScimAccountLink(Base):
    """SCIM external_id → local User link.

    Added in ``dom_005``. SCIM PUT/PATCH operations look the user up
    through this table so IDP-side renames or email changes don't
    strand the link. ``external_id`` is unique per tenant so a single
    IDP user can't shadow two local accounts; ``user_id`` is unique
    globally because a local user maps to one IDP identity.
    """

    __tablename__ = "scim_account_link"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "external_id", name="uq_scim_link_tenant_external"
        ),
        UniqueConstraint("user_id", name="uq_scim_link_user"),
    )


class AlertDomainConfig(Base):
    """Per-(tenant, domain) overrides for anomaly-detector thresholds.

    Added in ``dom_003``. The detector first reads this row for a given
    ``(tenant_id, domain)``; any NULL column falls back to the legacy
    single ``AlertChannelConfig`` row. Lets the CS sentiment-drop
    threshold differ from the Sales sentiment-drop threshold without
    refactoring the legacy table's tenant-only PK.

    Channel routing (in-app on/off, Slack on/off, Slack severity gate)
    stays on the legacy row — those are tenant-level, not per-motion.
    """

    __tablename__ = "alert_domain_config"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    domain: Mapped[str] = mapped_column(String(32), primary_key=True)
    topic_spike_pct_change_threshold: Mapped[Optional[int]] = mapped_column(Integer)
    topic_spike_min_volume: Mapped[Optional[int]] = mapped_column(Integer)
    sentiment_drop_threshold: Mapped[Optional[float]] = mapped_column(Float)
    churn_surge_multiplier: Mapped[Optional[float]] = mapped_column(Float)
    methodology_drop_threshold: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "domain IN ('sales', 'customer_service', 'it_support', 'generic')",
            name="ck_alert_domain_config_domain",
        ),
    )


class KBArticleRequest(Base):
    """KB-edit-request artifact for Support recommendations.

    Replaces the CoachingNote stub the ``update_kb_article`` /
    ``escalate_recurring_issue`` Apply paths created in PR #113. The
    Support manager (or KB owner) works these from a dedicated inbox
    at ``/kb/requests`` with its own lifecycle independent from coaching.
    """

    __tablename__ = "kb_article_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    source_recommendation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("manager_recommendations.id", ondelete="SET NULL")
    )
    # Optional anchor to an existing KB chunk this request is asking to
    # update; NULL when it's a request to create something new.
    source_kb_chunk_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("kb_chunks.id", ondelete="SET NULL")
    )
    topic: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    proposed_body: Mapped[Optional[str]] = mapped_column(Text)
    # open | in_progress | published | dismissed (CHECK in migration).
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open"
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium"
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismiss_reason: Mapped[Optional[str]] = mapped_column(Text)


class CoachingNote(Base):
    """Manager-to-rep coaching memo. Created either manually or via the
    one-click-apply path on a ``ManagerRecommendation`` with
    ``category='coach_rep'``. Kept separate from ``ActionItem`` because
    that table requires an ``interaction_id`` and a manager-level memo
    isn't anchored to one call.
    """

    __tablename__ = "coaching_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    assigned_to: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source_recommendation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("manager_recommendations.id", ondelete="SET NULL")
    )
    # open | done | dismissed
    status: Mapped[str] = mapped_column(String(16), default="open")
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SlackIntegration(Base):
    """Per-tenant Slack OAuth install. Stores the encrypted bot token
    and the channel chosen for manager-alert delivery.

    Token at-rest encryption uses ``backend.app.services.token_crypto``
    (same Fernet wrapper as the OAuth integrations table).
    """

    __tablename__ = "slack_integration"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    slack_team_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slack_team_name: Mapped[Optional[str]] = mapped_column(String(255))
    bot_user_id: Mapped[Optional[str]] = mapped_column(String(64))
    bot_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    default_channel_id: Mapped[Optional[str]] = mapped_column(String(64))
    default_channel_name: Mapped[Optional[str]] = mapped_column(String(255))
    # Per-domain channel override map (added in ``dom_003``). Shape:
    # ``{"sales": "C001", "customer_service": "C002", "it_support": "C003"}``.
    # When a key is missing, the fanout layer falls back to
    # ``default_channel_id``. Lets a tenant route Sales alerts to
    # ``#sales-alerts`` and Support alerts to ``#support-alerts``.
    domain_channel_map: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    installed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class LLMCallTelemetry(Base):
    """One row per Anthropic completion. Feeds the adaptive max_tokens
    ceiling — recompute_llm_ceilings reads this table and writes the
    aggregate ``llm_ceiling_recommendation`` row that ``compute_max_tokens``
    consults at request time."""

    __tablename__ = "llm_call_telemetry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_site: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(80))
    request_max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(32))
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_llm_telemetry_site_tier_created", "call_site", "tier", "created_at"),
        Index("ix_llm_telemetry_created", "created_at"),
    )


class LLMCeilingRecommendation(Base):
    """Recommended ``max_tokens`` per (call_site, tier). Recomputed nightly
    from ``llm_call_telemetry`` once a (call_site, tier) has ≥200 samples
    or 14 days of history. ``compute_max_tokens`` reads with an in-process
    LRU cache; until a row exists, the static per-tier ceiling applies."""

    __tablename__ = "llm_ceiling_recommendation"

    call_site: Mapped[str] = mapped_column(String(64), primary_key=True)
    tier: Mapped[str] = mapped_column(String(16), primary_key=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    p50: Mapped[int] = mapped_column(Integer, nullable=False)
    p95: Mapped[int] = mapped_column(Integer, nullable=False)
    p99: Mapped[int] = mapped_column(Integer, nullable=False)
    max_observed: Mapped[int] = mapped_column(Integer, nullable=False)
    truncation_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    recommended_ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InteractionStepRun(Base):
    """Durable per-step run ledger for the interaction pipeline.

    One row per (interaction, step, input-hash): the exactly-once backbone
    from docs/complexity/01-pipeline-exactly-once.md. A paid / non-idempotent
    pipeline step atomically claims its row before running (INSERT under the
    unique constraint; concurrent duplicates lose with IntegrityError), so
    retries and duplicate deliveries resume instead of re-running — a
    succeeded row means the step's output is already persisted and must be
    reused, never re-paid. Leases (``lease_expires_at``) let a takeover
    happen when the claiming worker died mid-step; ``attempt`` counts
    takeovers. All claim logic lives in ``services/pipeline_ledger.py`` —
    write through it, not directly.
    """

    __tablename__ = "interaction_step_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    # e.g. transcription | segmentation | analysis | scorecards |
    # entity_resolution | plan_synthesis — constants in pipeline_ledger.py.
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # sha256 hex of the step's canonical inputs; a changed input is a new run.
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # running | succeeded | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    claimed_by: Mapped[Optional[str]] = mapped_column(String(128))
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Optional digest/pointer describing where the output landed (the payload
    # itself stays on its natural home, e.g. Interaction.insights).
    output_digest: Mapped[Optional[str]] = mapped_column(String(256))
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "interaction_id", "step_key", "input_hash", name="uq_step_run_key"
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_step_run_status",
        ),
        Index("ix_step_runs_tenant_step_status", "tenant_id", "step_key", "status"),
    )


class AutoExecutionPolicy(Base):
    """Per-tenant, per-action-class dispatch policy for the governed
    auto-executor (``services/action_plan/executor.py``).

    Keyed by (tenant_id, action_class). No row for a class means
    ``manual`` — today's behavior, nothing auto-dispatches. Buckets:

    * ``low_risk``  — note, research, internal system_write.
    * ``high_risk`` — external email, CRM write (system_write to an
      external CRM), meeting/phone_call.

    ``mode``:
      * ``manual``           — default absent; the executor never touches
        these steps.
      * ``shadow``           — the executor logs "WOULD dispatch" + writes
        an audit ``StepResponse`` (``source='auto_executed'``) but
        performs NO external side effect and does not change step state.
      * ``approve_then_auto`` — the executor moves the step to
        ``pending_approval`` and stops; a human approves elsewhere.
      * ``auto``              — the executor dispatches for real, through
        the same code path as the manual endpoints.
    """

    __tablename__ = "auto_execution_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # 'low_risk' | 'high_risk' — see the class docstring for the bucket.
    action_class: Mapped[str] = mapped_column(String(32), nullable=False)
    # 'manual' | 'shadow' | 'approve_then_auto' | 'auto'.
    mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="manual", server_default="manual"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "action_class", name="uq_auto_execution_policy_class"
        ),
        CheckConstraint(
            "action_class IN ('low_risk', 'high_risk')",
            name="ck_auto_execution_policy_action_class",
        ),
        CheckConstraint(
            "mode IN ('manual', 'shadow', 'approve_then_auto', 'auto')",
            name="ck_auto_execution_policy_mode",
        ),
    )


# Wire the tenant-config cache invalidation listener once models are loaded.
# Importing at module bottom avoids a circular import (tenant_cache itself
# imports ``Tenant`` from this module).
def _register_tenant_cache_listener() -> None:
    try:
        from backend.app.services.tenant_cache import register_invalidation_listener

        register_invalidation_listener()
    except Exception:  # pragma: no cover — best-effort
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "tenant_cache listener registration skipped", exc_info=True
        )


_register_tenant_cache_listener()
