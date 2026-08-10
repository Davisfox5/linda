"""Reconcile ``ck_manager_alerts_kind`` drift and add campaign-monitor kinds.

Same failure class ``sen_001`` fixed for recommendation categories:
``aa01b2c3d4e5`` created the CHECK with only the four original sales
kinds, but six services now write ten more — the CS/Support anomaly
detectors (``renewal_risk_spike``, ``health_score_drop``,
``csat_drop_support``, ``escalation_surge``, ``ttr_drift``), the
support/sales/CS trend detectors (``recurring_issue_detected``,
``sales_trend_detected``, ``cs_trend_detected``), the commitment
detector (``broken_commitment_detected``) and concern aggregation
(``customer_concern_trend_detected``). On any DB where the original
CHECK is still live, every one of those INSERTs dies with
CheckViolation. No migration between ``aa01b2c3d4e5`` and this one
touches the constraint (``sen_001`` only widened the column to
String(48)), so this recreates it from the full universe — mirrored by
``models.MANAGER_ALERT_KINDS`` and guarded by
``tests/test_manager_alert_kinds.py`` — plus the six new campaign
monitor kinds.

Expand-only: the widened CHECK accepts every kind old code writes, so
it is safe under the Fly release flow (release_command migrates before
new code boots while old code still serves). The DROP uses IF EXISTS
because the drift means the live constraint state cannot be assumed —
it may have been altered or removed out-of-band.

Revision ID: cmp_001_campaign_kinds
Revises: out_003_flex_step_guidance
Create Date: 2026-08-10
"""

from alembic import op

# revision identifiers, used by Alembic.
# NOTE: revision ids must fit alembic_version.version_num VARCHAR(32)
# (see tests/test_migration_revision_ids.py and the sen_001 incident).
revision = "cmp_001_campaign_kinds"
down_revision = "out_003_flex_step_guidance"
branch_labels = None
depends_on = None


# Mirrors ``models.MANAGER_ALERT_KINDS`` — adding a kind there requires
# a follow-up migration extending this list.
_KINDS = (
    # sales anomaly scan (aa01b2c3d4e5 originals)
    "topic_spike",
    "sentiment_drop",
    "churn_surge",
    "methodology_drop",
    # customer-success anomaly scan
    "renewal_risk_spike",
    "health_score_drop",
    # support anomaly scan
    "csat_drop_support",
    "escalation_surge",
    "ttr_drift",
    # trend / commitment / concern detectors
    "recurring_issue_detected",
    "broken_commitment_detected",
    "sales_trend_detected",
    "cs_trend_detected",
    "customer_concern_trend_detected",
    # campaign monitor (campaign_monitor.py)
    "campaign_bounce_spike",
    "campaign_optout_spike",
    "campaign_no_engagement",
    "campaign_stalled",
    "campaign_quota_starved",
    "campaign_completed_summary",
)

# The pre-campaign universe: everything non-campaign code writes today.
# Downgrading to the original four-kind constraint would fail CHECK
# validation on any DB holding CS/Support/trend alert rows, so the
# downgrade target is this reconciled set instead.
_PRE_CAMPAIGN_KINDS = tuple(k for k in _KINDS if not k.startswith("campaign_"))


def _check_sql(kinds) -> str:
    return "kind IN (" + ", ".join("'%s'" % k for k in kinds) + ")"


def upgrade() -> None:
    # IF EXISTS: the live constraint may already have been dropped or
    # altered out-of-band while the drifted inserts were failing.
    op.execute(
        "ALTER TABLE manager_alerts DROP CONSTRAINT IF EXISTS ck_manager_alerts_kind"
    )
    op.create_check_constraint(
        "ck_manager_alerts_kind",
        "manager_alerts",
        _check_sql(_KINDS),
    )


def downgrade() -> None:
    # NOTE: only safe on a DB with no campaign_* kind rows (resolve or
    # delete campaign alerts first) — mirroring sen_001's guarded
    # downgrade posture.
    op.execute(
        "ALTER TABLE manager_alerts DROP CONSTRAINT IF EXISTS ck_manager_alerts_kind"
    )
    op.create_check_constraint(
        "ck_manager_alerts_kind",
        "manager_alerts",
        _check_sql(_PRE_CAMPAIGN_KINDS),
    )
