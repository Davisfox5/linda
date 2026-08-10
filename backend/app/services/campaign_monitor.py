"""Proactive campaign health monitor.

Sync (Celery-side), modeled on ``anomaly_detector.py`` /
``campaign_winner_service.py`` / ``tenant_insights_service.py``. Five
deterministic detectors (no LLM) scan every active-or-recently-ended
campaign per tenant, insert a ``ManagerAlert`` row per newly-detected
condition (dedup via the same partial-unique-fingerprint idiom
``anomaly_detector.py`` uses), then render a plain-English title/body
with one Haiku call per NEW alert. Detection and delivery never depend
on the LLM call succeeding — every alert is inserted with a
deterministic template title/body first; the Haiku call only upgrades
the copy in place.

Metric definitions (sent/opens/clicks/replies/bounces/unsubscribes/
conversions, member funnel, daily quota) mirror
``backend.app.services.campaign_stats`` (async, owned by the campaign-
visibility-chat work) — small private sync SQL helpers are reimplemented
here rather than importing that module, per the plan's accepted
sync/async duplication tradeoff. If those definitions change, update
both.

Cadence: hourly via ``campaign_monitor_scan_all_tenants`` in tasks.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pydantic import ValidationError

from backend.app.config import get_settings
from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    EmailSend,
    ManagerAlert,
    OutreachMember,
    Tenant,
)
from backend.app.services.model_router import (
    CacheableBlock,
    LLMRequest,
    TaskType,
    Tier,
    get_router,
)
from backend.app.services.outreach.common import local_day_bounds_utc, parse_config
from backend.app.services.plain_english import sanitize_manager_text

logger = logging.getLogger(__name__)


# ── Alert kind vocabulary ────────────────────────────────────────────────
#
# Exported so tests (and the migration guarding ck_manager_alerts_kind)
# can assert every kind this module emits is covered. Mirrors the
# campaign-kind slice of ``models.MANAGER_ALERT_KINDS`` (authored
# alongside migration ``cmp_001`` at the fable tier) — kept as a local
# tuple here rather than importing models.MANAGER_ALERT_KINDS so this
# module's own vocabulary is self-contained and doesn't silently grow if
# that superset tuple grows for unrelated (non-campaign) kinds.
CAMPAIGN_ALERT_KINDS = (
    "campaign_bounce_spike",
    "campaign_optout_spike",
    "campaign_no_engagement",
    "campaign_stalled",
    "campaign_quota_starved",
    "campaign_completed_summary",
)


# ── Thresholds (module-level constants) ──────────────────────────────────
#
# PROVISIONAL pending two weeks of real traffic — same posture as
# anomaly_detector's cadence comment (tasks.py:421-424). Deliberately
# cheap to change; not stored in AlertChannelConfig yet (that would be
# another migration).
BOUNCE_SPIKE_RATE = 0.05
BOUNCE_SPIKE_MIN_SENT = 20
OPTOUT_SPIKE_RATE = 0.02
OPTOUT_SPIKE_MIN_SENT = 20
NO_ENGAGEMENT_MIN_SENT = 30
NO_ENGAGEMENT_OPEN_RATE = 0.10
STALLED_LOOKBACK_DAYS = 3
# Pragmatic encoding of "persist across 2 consecutive hourly scans"
# without adding new persistent state — see ``_detect_quota_starved``.
QUOTA_STARVED_MIN_WINDOW_AGE_HOURS = 1

# Fast-skip / candidate-campaign / completion lookback window. Mirrors
# the plan's literal SQL: ``status='active' OR ended_at > now()-interval
# '2 days'``.
ACTIVE_OR_RECENT_LOOKBACK_DAYS = 2

# External-kind campaigns never leave status='active' (models.py:2446),
# so without a recency gate the scan would evaluate — and on the first
# run alert on — every external campaign ever ingested, forever, and
# those alerts could never auto-resolve. An external campaign is a
# candidate only while it started recently or is still receiving
# recent activity (``ended_at`` inside the completion lookback); alerts
# on campaigns that age out of this window are auto-resolved by
# ``_resolve_cleared``. Outreach campaigns keep the plan's literal
# status semantics — their scheduler walks them to ``completed``.
EXTERNAL_ACTIVE_WINDOW_DAYS = 30

# Non-terminal OutreachMember states — a member here is still "in
# flight" (enrolled, drafted, or mid-sequence) as opposed to a resolved
# outcome. Used by campaign_stalled's "pending/approved members" check
# and campaign_optout_spike's opted-out count.
_OUTREACH_TERMINAL_STATES = frozenset(
    {"replied", "bounced", "opted_out", "completed", "failed", "halted"}
)


@dataclass(frozen=True)
class DetectedCampaignAlert:
    kind: str
    severity: str
    title: str
    body: str
    evidence: Dict[str, Any]
    fingerprint: str
    campaign_id: Any
    # Campaigns are a sales motion (see anomaly_detector.DetectedAnomaly's
    # equivalent field) — drives Slack per-domain routing
    # (manager_alert_fanout.py:140-156).
    domain: str = "sales"


# ── Haiku rendering ───────────────────────────────────────────────────────

_HAIKU_SYSTEM_PROMPT = (
    "You rewrite a deterministic campaign-health alert into plain English "
    "for a sales manager. You are given the alert kind, severity, and the "
    "evidence dict a detector computed, plus a template title/body as a "
    "fallback baseline (already a faithful summary of the numbers).\n\n"
    "Write:\n"
    "- title: at most 25 words, plain English, states what happened with "
    "the key number.\n"
    "- body: 2-3 sentences. Sentence 1: what happened, citing the numbers "
    "from evidence. Sentence 2: why it matters. Then 1-2 concrete "
    "suggestions, each phrased as 'Suggestion: ...'.\n\n"
    "VOICE RULES: never invent a number that isn't in evidence. No "
    "em-dashes. Never say 'LINDA suggests' or brand the copy in any way "
    "(this ships to white-label tenants too) — always use the "
    "'Suggestion: ...' phrasing instead. Return ONLY a JSON object with "
    "keys title and body, no surrounding prose."
)


def _render_haiku(alert: ManagerAlert) -> None:
    """Upgrade one newly-inserted alert's title/body via a single Haiku
    call. Mutates ``alert`` in place. Never raises — on any failure
    (LLM call, parsing) the deterministic template title/body the
    detector already wrote stays as-is, so detection/delivery never
    depend on LLM availability."""
    try:
        prompt_body = {
            "kind": alert.kind,
            "severity": alert.severity,
            "evidence": alert.evidence,
            "template_title": alert.title,
            "template_body": alert.body,
        }
        resp = get_router().invoke(
            LLMRequest(
                task_type=TaskType.GENERIC,
                forced_tier=Tier.HAIKU,
                user_message=json.dumps(prompt_body, default=str),
                system_blocks=[CacheableBlock(text=_HAIKU_SYSTEM_PROMPT, cache=True)],
                max_tokens=400,
                temperature=0.0,
                call_site="campaign_monitor",
            )
        )
        text = resp.text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
        title = parsed.get("title") if isinstance(parsed, dict) else None
        body = parsed.get("body") if isinstance(parsed, dict) else None
        if isinstance(title, str) and title.strip():
            alert.title = sanitize_manager_text(title.strip(), max_words=25)
        if isinstance(body, str) and body.strip():
            alert.body = sanitize_manager_text(body.strip(), max_words=80)
    except Exception:
        logger.exception(
            "campaign_monitor: Haiku render failed for alert kind %s "
            "(keeping deterministic template copy)",
            alert.kind,
        )


# ── Public entry points ──────────────────────────────────────────────────


def scan_tenant(session: Session, tenant: Tenant) -> List[ManagerAlert]:
    """Run every detector + the completion wrap-up for one tenant.

    Cheap fast-skip first: if the tenant has no active-or-recently-ended
    campaign, returns immediately without touching anything else.
    Per-detector and per-campaign failures are non-fatal (logged,
    skipped) so one bad campaign can't blank out the whole tenant's scan.
    Commits once at the end; the fast-skip path stays read-only (safe —
    the ``after_begin`` listener re-arms the tenant GUC on every new
    transaction) unless it resolved orphaned alerts, in which case it
    commits that. The single commit lets the caller
    (``scan_all_tenants``) isolate one tenant's failure from the rest.
    """
    now = datetime.now(timezone.utc)
    if _active_or_recent_count(session, tenant.id, now) == 0:
        # No candidates — but open campaign alerts may still need
        # resolving (e.g. an external campaign that just aged out of
        # EXTERNAL_ACTIVE_WINDOW_DAYS took its alerts out of the
        # candidate set with it). Cheap: one indexed query when there
        # are no open campaign alerts.
        if _resolve_cleared(session, tenant, now):
            session.commit()
        return []

    campaigns = _candidate_campaigns(session, tenant.id, now)
    found: List[DetectedCampaignAlert] = []
    for campaign in campaigns:
        try:
            found.extend(_detect_for_campaign(session, tenant, campaign, now))
        except Exception:
            logger.exception(
                "campaign_monitor: detection failed for campaign %s (non-fatal)",
                campaign.id,
            )
        try:
            completion = _maybe_build_completion_report(session, campaign, now)
        except Exception:
            logger.exception(
                "campaign_monitor: completion wrap-up failed for campaign %s "
                "(non-fatal)",
                campaign.id,
            )
            completion = None
        if completion is not None:
            found.append(completion)

    inserted: List[ManagerAlert] = []
    for anomaly in found:
        row = _insert_alert(session, tenant.id, anomaly)
        if row is not None:
            _render_haiku(row)
            inserted.append(row)

    _resolve_cleared(session, tenant, now)
    session.commit()
    return inserted


def scan_all_tenants(session: Session) -> Dict[str, Any]:
    """Beat-task entrypoint. Iterates every tenant under its own
    ``tenant_context`` (RLS fail-closed — an unscoped query would
    silently see zero rows), collects per-tenant inserted-alert counts,
    never lets one tenant's failure abort the batch."""
    from backend.app.tenant_ctx import tenant_context

    tenants = session.execute(select(Tenant)).scalars().all()
    by_tenant: Dict[str, int] = {}
    total = 0
    for tenant in tenants:
        try:
            with tenant_context(tenant.id, session):
                inserted = scan_tenant(session, tenant)
            by_tenant[str(tenant.id)] = len(inserted)
            total += len(inserted)
        except Exception:
            logger.exception(
                "campaign_monitor: scan_tenant failed for tenant %s (non-fatal)",
                tenant.id,
            )
            # Clears the aborted-transaction state so the NEXT tenant's
            # queries don't inherit this one's failure (same reasoning as
            # tenant_insights_service.rollup_all_tenants_weekly).
            session.rollback()
            by_tenant[str(tenant.id)] = -1
    return {"tenants_scanned": len(tenants), "alerts_inserted": total, "by_tenant": by_tenant}


# ── Fast-skip / candidate campaigns ───────────────────────────────────────


def _lookback_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=ACTIVE_OR_RECENT_LOOKBACK_DAYS)


def _external_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=EXTERNAL_ACTIVE_WINDOW_DAYS)


def _candidate_filter(tenant_id: Any, now: datetime):
    """Shared WHERE clause for the fast-skip count and candidate select.

    Outreach campaigns follow the plan's literal SQL (``status='active'
    OR ended_at > lookback``) — their scheduler walks them to
    ``completed``. External campaigns are permanently 'active'
    (models.py:2446), so they additionally require recency (started or
    created inside EXTERNAL_ACTIVE_WINDOW_DAYS, or ended inside the
    completion lookback) — see the constant's comment. A completed
    campaign whose ``ended_at`` is NULL and older than the lookback is
    missed — accepted per the plan's literal query."""
    cutoff = _lookback_cutoff(now)
    ext_cutoff = _external_cutoff(now)
    recent = sa.or_(Campaign.status == "active", Campaign.ended_at > cutoff)
    return sa.and_(
        Campaign.tenant_id == tenant_id,
        recent,
        sa.or_(
            Campaign.kind != "external",
            func.coalesce(Campaign.started_at, Campaign.created_at) > ext_cutoff,
            Campaign.ended_at > cutoff,
        ),
    )


def _active_or_recent_count(session: Session, tenant_id: Any, now: datetime) -> int:
    """Cheap pre-check: does this tenant have anything worth scanning?"""
    return int(
        session.execute(
            select(func.count(Campaign.id)).where(_candidate_filter(tenant_id, now))
        ).scalar_one()
        or 0
    )


def _candidate_campaigns(session: Session, tenant_id: Any, now: datetime) -> List[Campaign]:
    return (
        session.execute(select(Campaign).where(_candidate_filter(tenant_id, now)))
        .scalars()
        .all()
    )


# ── Sync metric helpers ───────────────────────────────────────────────────
#
# Source of truth for these definitions is
# ``backend.app.services.campaign_stats`` (``compute_rollup`` /
# ``member_states`` / ``quota_state``) — async, owned by the campaign-
# visibility-chat work. Reimplemented here as small sync SQL helpers
# rather than imported, per the plan's accepted sync/async duplication
# tradeoff (2.2/2.9). Keep both in sync if the definitions change.


def _campaign_event_counts(session: Session, campaign_id: Any) -> Dict[str, int]:
    rows = session.execute(
        select(CampaignEvent.event_type, func.count(CampaignEvent.id))
        .where(CampaignEvent.campaign_id == campaign_id)
        .group_by(CampaignEvent.event_type)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


def _sent_count(session: Session, campaign_id: Any) -> int:
    return int(
        session.execute(
            select(func.count(CampaignRecipient.id)).where(
                CampaignRecipient.campaign_id == campaign_id
            )
        ).scalar_one()
        or 0
    )


def _member_state_counts(session: Session, campaign_id: Any) -> Dict[str, int]:
    rows = session.execute(
        select(OutreachMember.state, func.count(OutreachMember.id))
        .where(OutreachMember.campaign_id == campaign_id)
        .group_by(OutreachMember.state)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


def _email_sends_since(session: Session, campaign_id: Any, start: datetime) -> int:
    return int(
        session.execute(
            select(func.count(EmailSend.id)).where(
                EmailSend.campaign_id == campaign_id,
                EmailSend.status == "sent",
                EmailSend.created_at >= start,
            )
        ).scalar_one()
        or 0
    )


def _email_sends_in_window(
    session: Session, campaign_id: Any, start: datetime, end: datetime
) -> int:
    return int(
        session.execute(
            select(func.count(EmailSend.id)).where(
                EmailSend.campaign_id == campaign_id,
                EmailSend.status == "sent",
                EmailSend.created_at >= start,
                EmailSend.created_at < end,
            )
        ).scalar_one()
        or 0
    )


def _tenant_email_sends_in_window(
    session: Session, tenant_id: Any, start: datetime, end: datetime
) -> int:
    return int(
        session.execute(
            select(func.count(EmailSend.id)).where(
                EmailSend.tenant_id == tenant_id,
                EmailSend.status == "sent",
                EmailSend.campaign_id.is_not(None),
                EmailSend.created_at >= start,
                EmailSend.created_at < end,
            )
        ).scalar_one()
        or 0
    )


# ── Detectors ──────────────────────────────────────────────────────────
#
# Uniform signature ``(session, tenant, campaign, counts, sent, now) ->
# Optional[DetectedCampaignAlert]`` so ``_condition_still_true`` (used by
# resolution) can re-run any of them with fresh numbers.


def _detect_bounce_spike(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    counts: Dict[str, int],
    sent: int,
    now: datetime,
) -> Optional[DetectedCampaignAlert]:
    """Deliverability damage compounds, so this fires at high severity.
    Applies to both campaign kinds — both reuse campaign_events."""
    if sent < BOUNCE_SPIKE_MIN_SENT:
        return None
    bounces = counts.get("bounce", 0)
    rate = bounces / sent
    if rate < BOUNCE_SPIKE_RATE:
        return None
    evidence = {
        "campaign_id": str(campaign.id),
        "sent": sent,
        "bounces": bounces,
        "rate": round(rate, 4),
        "threshold": BOUNCE_SPIKE_RATE,
    }
    title = sanitize_manager_text(
        f"Campaign '{campaign.name}' bounce rate hit {rate:.1%} ({bounces}/{sent} sent).",
        max_words=25,
    )
    body = (
        f"{bounces} of {sent} sends bounced ({rate:.1%}), above the "
        f"{BOUNCE_SPIKE_RATE:.0%} threshold. Suggestion: pause the campaign "
        f"and re-verify the recipient list before resuming."
    )
    return DetectedCampaignAlert(
        kind="campaign_bounce_spike",
        severity="high",
        title=title,
        body=body,
        evidence=evidence,
        fingerprint=_fingerprint("campaign_bounce_spike", str(campaign.id)),
        campaign_id=campaign.id,
    )


def _detect_optout_spike(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    counts: Dict[str, int],
    sent: int,
    now: datetime,
) -> Optional[DetectedCampaignAlert]:
    """(unsubscribe events + opted-out outreach members) / sent. Applies
    to both kinds; the opted-out-member term is 0 for external campaigns
    (no ``outreach_members`` rows exist for them)."""
    if sent < OPTOUT_SPIKE_MIN_SENT:
        return None
    unsubs = counts.get("unsubscribe", 0)
    opted_out_members = 0
    if campaign.kind == "outreach":
        opted_out_members = _member_state_counts(session, campaign.id).get("opted_out", 0)
    total = unsubs + opted_out_members
    rate = total / sent
    if rate < OPTOUT_SPIKE_RATE:
        return None
    evidence = {
        "campaign_id": str(campaign.id),
        "sent": sent,
        "unsubscribes": unsubs,
        "opted_out_members": opted_out_members,
        "rate": round(rate, 4),
        "threshold": OPTOUT_SPIKE_RATE,
    }
    title = sanitize_manager_text(
        f"Campaign '{campaign.name}' opt-out rate hit {rate:.1%} ({total}/{sent} sent).",
        max_words=25,
    )
    body = (
        f"{total} of {sent} recipients opted out or unsubscribed ({rate:.1%}), "
        f"above the {OPTOUT_SPIKE_RATE:.0%} threshold. Suggestion: review the "
        f"messaging and offer cadence before sending more from this list."
    )
    return DetectedCampaignAlert(
        kind="campaign_optout_spike",
        severity="high",
        title=title,
        body=body,
        evidence=evidence,
        fingerprint=_fingerprint("campaign_optout_spike", str(campaign.id)),
        campaign_id=campaign.id,
    )


def _detect_no_engagement(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    counts: Dict[str, int],
    sent: int,
    now: datetime,
) -> Optional[DetectedCampaignAlert]:
    """sent >= 30 (reuses the winner-service >=30-sends precedent), zero
    replies, and — for external campaigns only — open rate < 10%.

    The "for external-with-open-tracking" qualifier from the plan
    collapses to simply checking the open rate for external campaigns:
    a campaign with no open-tracking data has 0 opens, so its open rate
    is trivially 0% (< 10%) and the alert still fires on replies alone,
    identical to skipping the check entirely; a campaign WITH real
    tracked opens above the bar is correctly read as "engaged even
    without a reply" and does not fire. Outreach campaigns skip the
    open-rate qualifier altogether (opens aren't the outreach engine's
    primary engagement signal — replies are)."""
    if sent < NO_ENGAGEMENT_MIN_SENT:
        return None
    replies = counts.get("reply", 0)
    if replies > 0:
        return None
    open_rate: Optional[float] = None
    if campaign.kind == "external":
        opens = counts.get("open", 0)
        open_rate = opens / sent
        if open_rate >= NO_ENGAGEMENT_OPEN_RATE:
            return None
    evidence = {
        "campaign_id": str(campaign.id),
        "sent": sent,
        "replies": replies,
        "open_rate": round(open_rate, 4) if open_rate is not None else None,
        "threshold_open_rate": NO_ENGAGEMENT_OPEN_RATE,
    }
    title = sanitize_manager_text(
        f"Campaign '{campaign.name}' has zero replies after {sent} sends.",
        max_words=25,
    )
    body = (
        f"{sent} sent, 0 replies so far"
        + (f", {open_rate:.1%} open rate" if open_rate is not None else "")
        + ". Suggestion: rework the subject line or opening hook, and "
        "consider a smaller test segment before sending the rest of the list."
    )
    return DetectedCampaignAlert(
        kind="campaign_no_engagement",
        severity="medium",
        title=title,
        body=body,
        evidence=evidence,
        fingerprint=_fingerprint("campaign_no_engagement", str(campaign.id)),
        campaign_id=campaign.id,
    )


def _detect_stalled(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    counts: Dict[str, int],
    sent: int,
    now: datetime,
) -> Optional[DetectedCampaignAlert]:
    """Outreach only: campaign is active, has non-terminal ("pending/
    approved") members, but nothing has sent in 3 days — the sequence
    isn't progressing even though there's work queued."""
    if campaign.kind != "outreach" or campaign.status != "active":
        return None
    states = _member_state_counts(session, campaign.id)
    pending = sum(c for state, c in states.items() if state not in _OUTREACH_TERMINAL_STATES)
    if pending <= 0:
        return None
    cutoff = now - timedelta(days=STALLED_LOOKBACK_DAYS)
    recent_sends = _email_sends_since(session, campaign.id, cutoff)
    if recent_sends > 0:
        return None
    evidence = {
        "campaign_id": str(campaign.id),
        "pending_members": pending,
        "member_states": states,
        "lookback_days": STALLED_LOOKBACK_DAYS,
    }
    title = sanitize_manager_text(
        f"Campaign '{campaign.name}' has stalled: {pending} prospects waiting, "
        f"no sends in {STALLED_LOOKBACK_DAYS} days.",
        max_words=25,
    )
    body = (
        f"{pending} prospects are still in the sequence but no touch has gone "
        f"out in {STALLED_LOOKBACK_DAYS} days. Suggestion: check pending drafts "
        f"for approval, and confirm the connected mailbox is still authorized."
    )
    return DetectedCampaignAlert(
        kind="campaign_stalled",
        severity="medium",
        title=title,
        body=body,
        evidence=evidence,
        fingerprint=_fingerprint("campaign_stalled", str(campaign.id)),
        campaign_id=campaign.id,
    )


def _detect_quota_starved(
    session: Session,
    tenant: Tenant,
    campaign: Campaign,
    counts: Dict[str, int],
    sent: int,
    now: datetime,
) -> Optional[DetectedCampaignAlert]:
    """Outreach only: this campaign sent 0 today while the tenant-wide
    daily send cap is already exhausted by other campaigns.

    Persistence design: the plan asks for "persisting across 2
    consecutive scans" to avoid firing on a single noisy reading, but
    encoding that literally requires new state (a stored "first seen
    starved at" timestamp) which is out of scope here. Instead we
    require the campaign's local quota day to be more than one scan
    interval (``QUOTA_STARVED_MIN_WINDOW_AGE_HOURS`` = 1h, matching the
    hourly cadence) old before trusting a zero-sent reading: a campaign
    whose local day just rolled over cannot yet have been observed
    starved on a prior scan, so we wait. By the time the day is >1h old,
    any campaign still reading zero-sent-while-tenant-capped has
    necessarily been in that state across at least one full scan
    interval — an equivalent noise floor to "2 consecutive scans"
    without persisting extra state.
    """
    if campaign.kind != "outreach" or campaign.status != "active":
        return None
    try:
        config = parse_config(campaign.config)
    except ValidationError:
        return None
    day_start, day_end = local_day_bounds_utc(config.send_window, now)
    if now - day_start < timedelta(hours=QUOTA_STARVED_MIN_WINDOW_AGE_HOURS):
        return None
    campaign_sent_today = _email_sends_in_window(session, campaign.id, day_start, day_end)
    if campaign_sent_today > 0:
        return None
    tenant_sent_today = _tenant_email_sends_in_window(session, tenant.id, day_start, day_end)
    cap = get_settings().OUTREACH_TENANT_DAILY_SEND_CAP
    if tenant_sent_today < cap:
        return None
    evidence = {
        "campaign_id": str(campaign.id),
        "campaign_sent_today": campaign_sent_today,
        "tenant_sent_today": tenant_sent_today,
        "tenant_daily_cap": cap,
        "day_start": day_start.isoformat(),
    }
    title = sanitize_manager_text(
        f"Campaign '{campaign.name}' hasn't sent today; the tenant daily send "
        f"cap is exhausted.",
        max_words=25,
    )
    body = (
        f"This campaign sent 0 emails today while other campaigns used the "
        f"full tenant daily cap ({tenant_sent_today}/{cap}). Suggestion: raise "
        f"the tenant daily cap, or stagger campaigns so this one gets a slice "
        f"of the quota."
    )
    return DetectedCampaignAlert(
        kind="campaign_quota_starved",
        severity="low",
        title=title,
        body=body,
        evidence=evidence,
        fingerprint=_fingerprint("campaign_quota_starved", str(campaign.id)),
        campaign_id=campaign.id,
    )


_DETECTOR_BY_KIND: Dict[str, Callable] = {
    "campaign_bounce_spike": _detect_bounce_spike,
    "campaign_optout_spike": _detect_optout_spike,
    "campaign_no_engagement": _detect_no_engagement,
    "campaign_stalled": _detect_stalled,
    "campaign_quota_starved": _detect_quota_starved,
}


def _detect_for_campaign(
    session: Session, tenant: Tenant, campaign: Campaign, now: datetime
) -> List[DetectedCampaignAlert]:
    """Run every applicable detector for one campaign. Outreach-only
    detectors (stalled, quota_starved) guard themselves on
    ``campaign.kind`` so external campaigns skip them; bounce/optout/
    no_engagement apply to both kinds (all reuse campaign_events)."""
    counts = _campaign_event_counts(session, campaign.id)
    sent = _sent_count(session, campaign.id)
    found: List[DetectedCampaignAlert] = []
    for name, detector in _DETECTOR_BY_KIND.items():
        try:
            result = detector(session, tenant, campaign, counts, sent, now)
        except Exception:
            logger.exception(
                "campaign_monitor: detector %s failed for campaign %s (non-fatal)",
                name,
                campaign.id,
            )
            continue
        if result is not None:
            found.append(result)
    return found


# ── Completion wrap-up report ─────────────────────────────────────────────


def _maybe_build_completion_report(
    session: Session, campaign: Campaign, now: datetime
) -> Optional[DetectedCampaignAlert]:
    """Scan-detected completion (status='completed', or ended_at within
    the lookback) that hasn't been reported yet. Idempotent by
    construction: once ``insights["completion_report"]`` exists, every
    later scan no-ops on this campaign. Writes the report into
    ``Campaign.insights`` (existing JSONB column, no migration) and
    returns the ``campaign_completed_summary`` alert to insert."""
    insights = campaign.insights or {}
    if "completion_report" in insights:
        return None
    completed = campaign.status == "completed" or (
        campaign.ended_at is not None and campaign.ended_at >= _lookback_cutoff(now)
    )
    if not completed:
        return None

    counts = _campaign_event_counts(session, campaign.id)
    sent = _sent_count(session, campaign.id)
    replies = counts.get("reply", 0)
    reply_rate = (replies / sent) if sent else 0.0
    rollup = {
        "sent": sent,
        "opens": counts.get("open", 0),
        "clicks": counts.get("click", 0),
        "replies": replies,
        "bounces": counts.get("bounce", 0),
        "unsubscribes": counts.get("unsubscribe", 0),
        "conversions": counts.get("convert", 0),
    }
    funnel = _member_state_counts(session, campaign.id) if campaign.kind == "outreach" else None

    title = sanitize_manager_text(
        f"Campaign '{campaign.name}' finished: {sent} sent, {replies} replies "
        f"({reply_rate:.1%}).",
        max_words=25,
    )
    body_bits = [
        f"{sent} sent, {rollup['opens']} opens, {replies} replies "
        f"({reply_rate:.1%}), {rollup['bounces']} bounces."
    ]
    if funnel:
        body_bits.append(
            "Funnel: " + ", ".join(f"{k}={v}" for k, v in sorted(funnel.items())) + "."
        )
    body_bits.append(
        "Suggestion: review the reply thread for what resonated before the next run."
    )
    body = " ".join(body_bits)

    report = {
        "generated_at": now.isoformat(),
        "rollup": rollup,
        "funnel": funnel,
        "reply_rate": round(reply_rate, 4),
        "narrative": {"title": title, "body": body},
    }
    # Dict-copy reassignment (not in-place mutation) so SQLAlchemy's
    # change tracking picks it up without needing flag_modified — same
    # convention interactions.py uses for JSONB ``insights`` writes.
    new_insights = dict(insights)
    new_insights["completion_report"] = report
    campaign.insights = new_insights

    evidence = {
        "campaign_id": str(campaign.id),
        "campaign_name": campaign.name,
        "kind": campaign.kind,
        "rollup": rollup,
        "funnel": funnel,
        "reply_rate": round(reply_rate, 4),
    }
    return DetectedCampaignAlert(
        kind="campaign_completed_summary",
        severity="low",
        title=title,
        body=body,
        evidence=evidence,
        fingerprint=_fingerprint("campaign_completed_summary", str(campaign.id)),
        campaign_id=campaign.id,
    )


# ── Resolution ─────────────────────────────────────────────────────────
#
# Implemented here (not in anomaly_detector.resolve_stale) per the
# plan's recommendation: a per-scan recheck of the same detector that
# fired, rather than teaching the generic time-based resolver
# campaign-specific conditions.


def _resolve_cleared(session: Session, tenant: Tenant, now: datetime) -> int:
    """Resolve open campaign alerts whose triggering condition has
    cleared, or whose campaign has completed. Conservative like
    anomaly_detector._still_active: when the campaign or its detector
    can't be resolved, the alert is left open for a human to dismiss."""
    resolved = 0
    open_alerts = (
        session.execute(
            select(ManagerAlert).where(
                ManagerAlert.tenant_id == tenant.id,
                ManagerAlert.kind.in_(CAMPAIGN_ALERT_KINDS),
                ManagerAlert.resolved_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for alert in open_alerts:
        if alert.kind == "campaign_completed_summary":
            # One-shot report alert — nothing to "clear".
            continue
        campaign_id = (alert.evidence or {}).get("campaign_id")
        campaign = None
        if campaign_id:
            try:
                # evidence is JSONB — campaign_id round-trips as a plain
                # str, not the uuid.UUID instance the mapped PK expects.
                campaign = session.get(Campaign, uuid.UUID(str(campaign_id)))
            except (ValueError, TypeError):
                campaign = None
        if campaign is None:
            continue
        if campaign.status == "completed":
            alert.resolved_at = now
            resolved += 1
            continue
        if campaign.kind == "external" and not _external_still_candidate(campaign, now):
            # External campaigns never reach 'completed' — once one ages
            # out of EXTERNAL_ACTIVE_WINDOW_DAYS the scan stops
            # evaluating it, so its alerts must resolve here or they
            # would stay open forever.
            alert.resolved_at = now
            resolved += 1
            continue
        if not _condition_still_true(session, tenant, campaign, alert, now):
            alert.resolved_at = now
            resolved += 1
    return resolved


def _external_still_candidate(campaign: Campaign, now: datetime) -> bool:
    started = _aware(campaign.started_at or campaign.created_at)
    if started is not None and started > _external_cutoff(now):
        return True
    ended = _aware(campaign.ended_at)
    return ended is not None and ended > _lookback_cutoff(now)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite (unit tests) returns naive datetimes for tz-aware columns;
    Postgres returns aware ones. Same coercion idiom as
    ``pipeline_ledger``/``outreach/links``."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _condition_still_true(
    session: Session, tenant: Tenant, campaign: Campaign, alert: ManagerAlert, now: datetime
) -> bool:
    detector = _DETECTOR_BY_KIND.get(alert.kind)
    if detector is None:
        return True  # unknown kind — conservative, leave open
    counts = _campaign_event_counts(session, campaign.id)
    sent = _sent_count(session, campaign.id)
    return detector(session, tenant, campaign, counts, sent, now) is not None


# ── Alert insert (guarded dedup) ──────────────────────────────────────────


def _insert_alert(
    session: Session, tenant_id: Any, anomaly: DetectedCampaignAlert
) -> Optional[ManagerAlert]:
    """Insert one alert, deduping against any active fingerprint. Mirrors
    ``anomaly_detector._insert_alert``'s guarded-insert idiom — the
    Postgres partial unique index is the durable correctness layer; this
    pre-check keeps dedupe consistent under SQLite in unit tests (which
    doesn't honor a partial-index WHERE clause the same way)."""
    existing = session.execute(
        select(ManagerAlert.id).where(
            ManagerAlert.tenant_id == tenant_id,
            ManagerAlert.fingerprint == anomaly.fingerprint,
            ManagerAlert.resolved_at.is_(None),
        )
    ).first()
    if existing is not None:
        return None
    row = ManagerAlert(
        tenant_id=tenant_id,
        kind=anomaly.kind,
        severity=anomaly.severity,
        title=anomaly.title,
        body=anomaly.body,
        evidence=anomaly.evidence,
        fingerprint=anomaly.fingerprint,
        domain=anomaly.domain,
    )
    session.add(row)
    try:
        session.flush()
        return row
    except IntegrityError:
        session.rollback()
        return None


def _fingerprint(kind: str, subject: str) -> str:
    return hashlib.sha256(f"{kind}::{subject.lower()}".encode("utf-8")).hexdigest()[:32]
