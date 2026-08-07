"""Unified plan catalog — customer-facing tiers + seat/feature enforcement.

Single source of truth for what every tier can and cannot do. The
frontend reads ``/api/v1/me`` to discover the active tenant's limits
and hides or disables UI accordingly; the backend uses
``require_feature`` as a FastAPI dependency to reject locked requests
with 402, and the Stripe webhook calls :func:`apply_tier` to update a
tenant after a subscription change.

One ``Tenant.plan_tier`` column; one place to change a tier; one source
of truth for seat limits, usage caps, feature flags, and AI model tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException

from backend.app.auth import get_current_tenant
from backend.app.models import Tenant
from backend.app.services.entitlements import COMP_TIER, tenant_is_comped


PLAN_TIERS = ("sandbox", "starter", "growth", "enterprise")
_DEFAULT_TIER = "sandbox"


# ── Legacy tier-key mapping ───────────────────────────────────────────
# The pre-consolidation ``subscription_tier`` column used solo/team/pro
# keys. We keep this map so data-migration code and any still-in-flight
# Stripe price rules can translate without an outage.
LEGACY_TIER_ALIASES: Dict[str, str] = {
    "solo": "sandbox",
    "team": "starter",
    "pro": "growth",
    "enterprise": "enterprise",
}


@dataclass(frozen=True)
class TierSpec:
    """Per-tier feature flags, seat limits, and usage caps.

    Changing a tenant's tier is a **non-retroactive** operation: we
    never deactivate users even if the new tier's ``seat_limit`` is
    below the current active count. The UI surfaces that mismatch so
    the admin can decide who to off-board.
    """

    key: str
    label: str
    description: str
    # Seat enforcement (admin_seat_limit ≤ seat_limit; admins count toward seats).
    seat_limit: int
    admin_seat_limit: int
    # Usage caps — None means unlimited.
    max_monthly_minutes: Optional[int]
    max_uploads_per_day: Optional[int]
    # AI model tier for post-call analysis: haiku | sonnet | opus
    ai_model_tier: str
    # Feature flags merged into ``Tenant.features_enabled`` on apply_tier().
    # Keys not listed here are preserved (manual admin overrides survive).
    features: Dict[str, Any] = field(default_factory=dict)


# Ordered cheapest → most expensive for UI rendering.
PLANS: Dict[str, TierSpec] = {
    "sandbox": TierSpec(
        key="sandbox",
        label="Sandbox",
        description="3 seats, 120 min/month. Post-call analysis with Haiku.",
        seat_limit=3,
        admin_seat_limit=1,
        max_monthly_minutes=120,
        max_uploads_per_day=10,
        ai_model_tier="haiku",
        features={
            # Customer-facing gates (served to the UI via /me).
            "real_time_transcription": False,
            "live_coaching": False,
            "crm_push": False,
            "custom_scorecards": False,
            "custom_branding": False,
            "ask_linda": True,
            "api_access": False,
            "live_transcription_api": False,
            "embedded_transcripts": False,
            "meeting_assist": False,
            # Ops-side toggles (set on Tenant.features_enabled).
            "live_sentiment": False,
            "live_kb_retrieval": True,
            "keyterm_prompting": False,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": False,
        },
    ),
    "starter": TierSpec(
        key="starter",
        label="Starter",
        description="10 seats, 2k min/month. Adds CRM push + daily CRM sync.",
        seat_limit=10,
        admin_seat_limit=1,
        max_monthly_minutes=2000,
        max_uploads_per_day=None,
        ai_model_tier="sonnet",
        features={
            "real_time_transcription": False,
            "live_coaching": False,
            "crm_push": True,
            "custom_scorecards": False,
            "custom_branding": False,
            "ask_linda": True,
            "api_access": True,
            "live_transcription_api": False,
            "embedded_transcripts": False,
            "meeting_assist": False,
            "live_sentiment": False,
            "live_kb_retrieval": True,
            "keyterm_prompting": False,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": True,
        },
    ),
    "growth": TierSpec(
        key="growth",
        label="Growth",
        description=(
            "50 seats, 10k min/month. Adds real-time transcription, live "
            "coaching, live sentiment, Deepgram keyterm prompting, and "
            "custom scorecards."
        ),
        seat_limit=50,
        admin_seat_limit=3,
        max_monthly_minutes=10_000,
        max_uploads_per_day=None,
        ai_model_tier="sonnet",
        features={
            "real_time_transcription": True,
            "live_coaching": True,
            "crm_push": True,
            "custom_scorecards": True,
            "custom_branding": False,
            "ask_linda": True,
            "api_access": True,
            "live_transcription_api": False,
            "embedded_transcripts": False,
            "meeting_assist": False,
            "live_sentiment": True,
            "live_kb_retrieval": True,
            "keyterm_prompting": True,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": True,
        },
    ),
    "enterprise": TierSpec(
        key="enterprise",
        label="Enterprise",
        description="500 seats, 20 admins. Unlimited usage. Full feature set.",
        seat_limit=500,
        admin_seat_limit=20,
        max_monthly_minutes=None,
        max_uploads_per_day=None,
        ai_model_tier="opus",
        features={
            "real_time_transcription": True,
            "live_coaching": True,
            "crm_push": True,
            "custom_scorecards": True,
            "custom_branding": True,
            "ask_linda": True,
            "api_access": True,
            "live_transcription_api": True,
            "embedded_transcripts": True,
            "meeting_assist": True,
            "live_sentiment": True,
            "live_kb_retrieval": True,
            "keyterm_prompting": True,
            "infer_from_sources_autorun": True,
            "crm_sync_autorun": True,
        },
    ),
}


def normalize_tier_key(key: Optional[str]) -> str:
    """Map a legacy tier key (solo/team/pro) into its sandbox/starter/growth
    equivalent, and fall back to the default for unknown keys."""
    if not key:
        return _DEFAULT_TIER
    key = key.lower()
    if key in PLANS:
        return key
    return LEGACY_TIER_ALIASES.get(key, _DEFAULT_TIER)


def default_tier() -> TierSpec:
    return PLANS[_DEFAULT_TIER]


def get_tier(key: Optional[str]) -> TierSpec:
    """Return the tier spec for ``key`` (normalized). Unknown → default."""
    return PLANS[normalize_tier_key(key)]


def limits_for(tenant: Tenant) -> TierSpec:
    # Comped accounts are treated as top-tier everywhere limits are read —
    # all features, strongest model, no seat/usage caps.
    if tenant_is_comped(tenant):
        return get_tier(COMP_TIER)
    return get_tier(getattr(tenant, "plan_tier", None))


def list_tiers() -> List[Dict[str, Any]]:
    """Return the ordered tier catalog for the admin UI picker."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "description": spec.description,
            "seat_limit": spec.seat_limit,
            "admin_seat_limit": spec.admin_seat_limit,
            "max_monthly_minutes": spec.max_monthly_minutes,
            "max_uploads_per_day": spec.max_uploads_per_day,
            "ai_model_tier": spec.ai_model_tier,
            "features": dict(spec.features),
        }
        for spec in PLANS.values()
    ]


def apply_tier(tenant: Tenant, tier_key: str) -> TierSpec:
    """Apply a tier to a tenant in place. Returns the resolved spec.

    Side effects on ``tenant`` (caller must flush/commit):

    - ``plan_tier`` = resolved key (legacy keys auto-mapped; unknown
      keys fall back to the default with a stable label).
    - ``seat_limit`` / ``admin_seat_limit`` replaced with the tier's values.
    - ``features_enabled`` gets the tier's feature flags merged in
      (keys not in the tier catalog are preserved).

    Intentionally does NOT touch existing users — a downgrade below
    the current headcount is the admin's problem to resolve via the
    seat-reconciliation flow.
    """
    spec = get_tier(tier_key)
    tenant.plan_tier = spec.key
    tenant.seat_limit = spec.seat_limit
    tenant.admin_seat_limit = spec.admin_seat_limit
    merged = dict(tenant.features_enabled or {})
    for k, v in spec.features.items():
        merged[k] = v
    tenant.features_enabled = merged
    return spec


# ── Trial helpers ─────────────────────────────────────────────────────


def trial_is_active(tenant: Tenant) -> bool:
    if tenant.plan_tier != "sandbox" or tenant.trial_ends_at is None:
        return False
    return tenant.trial_ends_at > datetime.now(timezone.utc)


def trial_is_expired(tenant: Tenant) -> bool:
    # Comped accounts never count as trial-expired.
    if tenant_is_comped(tenant):
        return False
    return (
        tenant.plan_tier == "sandbox"
        and tenant.trial_ends_at is not None
        and tenant.trial_ends_at <= datetime.now(timezone.utc)
    )


# ── FastAPI dependency factory ────────────────────────────────────────


def require_feature(flag: str):
    """Return a FastAPI dependency that 402s if the current tenant lacks ``flag``.

    Usage::

        @router.post(
            "/calls/live",
            dependencies=[Depends(require_feature("real_time_transcription"))],
        )
        async def start_live_call(...): ...
    """

    async def _guard(tenant: Tenant = Depends(get_current_tenant)) -> Tenant:
        if tenant_is_comped(tenant):
            return tenant
        limits = limits_for(tenant)
        if not bool(limits.features.get(flag, False)):
            raise HTTPException(
                status_code=402,
                detail=f"Your plan does not include '{flag}'. Upgrade to unlock.",
            )
        if trial_is_expired(tenant):
            raise HTTPException(
                status_code=402,
                detail="Your sandbox trial has ended. Pick a plan to keep going.",
            )
        return tenant

    return _guard


# ── Revenue-endpoint guard (separate from feature gating) ─────────────


async def require_active_subscription(
    tenant: Tenant = Depends(get_current_tenant),
) -> Tenant:
    """Reject revenue-burning requests when the tenant is unpaid.

    Cheaper than ``require_feature`` — doesn't care which feature flags
    are toggled, just whether the tenant currently has a way to pay.
    Apply this to any endpoint that triggers Deepgram / Anthropic
    spend (uploads, ingests, analytics queries) so an expired sandbox
    trial can't keep burning credits.

    Read-only dashboard endpoints (``/me``, ``GET /interactions``)
    deliberately stay open so the SPA can render the trial-expired
    banner and an upgrade CTA.

    Currently 402s when:

    * the tenant is on ``sandbox`` AND ``trial_ends_at`` has passed; OR
    * the tenant is on a paid tier (``starter``/``growth``/``enterprise``)
      with no ``stripe_subscription_id`` linked — i.e., a paid plan
      that lost its subscription (cancelled, never wired up).

    Comped accounts bypass this gate entirely (full free access).
    """
    if tenant_is_comped(tenant):
        return tenant
    if trial_is_expired(tenant):
        raise HTTPException(
            status_code=402,
            detail="Your sandbox trial has ended. Pick a plan to keep going.",
        )
    if tenant.plan_tier != "sandbox" and not tenant.stripe_subscription_id:
        raise HTTPException(
            status_code=402,
            detail=(
                "No active subscription on this tenant. Resolve billing "
                "in /billing to resume usage."
            ),
        )
    return tenant
