"""Guard: the manager-alert kind universe can't silently drift again.

Same class as the recommendation-category incident (Sentry
LINDA-STAGING-2T, see tests/test_manager_recommendation_categories.py):
``ck_manager_alerts_kind`` shipped with four kinds while six services
grew ten more, so those INSERTs die with CheckViolation wherever the
original CHECK is live. The constraint is now generated from
``models.MANAGER_ALERT_KINDS`` (migration ``cmp_001``); these tests
fail the build when any writer's kind escapes that tuple, or when the
migration's copy falls out of sync with the model's.

Writer kinds are collected by scanning source text rather than
importing the detector modules (some drag heavy dependencies), matching
the grep-style guard in tests/test_model_catalog.py.
"""

from __future__ import annotations

import importlib.util
import os
import re

from backend.app.models import MANAGER_ALERT_KINDS

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_REPO, "backend", "app", "services")

# Every module that inserts ManagerAlert rows (directly or through
# trend_engine.persist_alerts). Adding a writer? Add it here too.
_WRITER_FILES = (
    "anomaly_detector.py",
    "support_trend_detector.py",
    "commitment_detector.py",
    "sales_trend_detector.py",
    "cs_trend_detector.py",
    "concern_aggregation.py",
    "campaign_monitor.py",
)


def _load_cmp_001():
    path = os.path.join(
        _REPO,
        "backend",
        "alembic",
        "versions",
        "cmp_001_campaign_alert_kinds.py",
    )
    spec = importlib.util.spec_from_file_location("cmp_001", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _writer_kinds(filename: str):
    """Kind strings a writer module can stamp on a ManagerAlert.

    Collects ``kind="literal"`` call-site arguments and module-level
    ``ALERT_KIND = "..."`` / ``ALERT_KINDS``-style tuple constants.
    """
    with open(os.path.join(_SERVICES, filename)) as fh:
        src = fh.read()
    kinds = set(re.findall(r'\bkind="([a-z0-9_]+)"', src))
    kinds.update(re.findall(r'^ALERT_KIND\s*=\s*"([a-z0-9_]+)"', src, re.M))
    tuple_blocks = re.findall(
        r"^CAMPAIGN_ALERT_KINDS[^=]*=\s*\(([^)]*)\)", src, re.M | re.S
    )
    for block in tuple_blocks:
        kinds.update(re.findall(r'"([a-z0-9_]+)"', block))
    return kinds


def test_every_writer_kind_is_in_models_tuple():
    allowed = set(MANAGER_ALERT_KINDS)
    for filename in _WRITER_FILES:
        emitted = _writer_kinds(filename)
        assert emitted, "no kinds found in %s — regex drift?" % filename
        missing = emitted - allowed
        assert not missing, (
            "%s writes ManagerAlert kinds missing from "
            "models.MANAGER_ALERT_KINDS (add them AND ship a migration "
            "recreating ck_manager_alerts_kind): %s"
            % (filename, sorted(missing))
        )


def test_migration_check_matches_models_tuple():
    mod = _load_cmp_001()
    assert tuple(mod._KINDS) == tuple(MANAGER_ALERT_KINDS), (
        "cmp_001's _KINDS is out of sync with models.MANAGER_ALERT_KINDS — "
        "a kind added to one side needs a follow-up migration keeping the "
        "DB CHECK and the model tuple identical"
    )


def test_kinds_fit_column_width():
    # manager_alerts.kind is String(48) (widened by sen_001).
    too_long = [k for k in MANAGER_ALERT_KINDS if len(k) > 48]
    assert not too_long, too_long


def test_campaign_kinds_present():
    # The campaign monitor's whole vocabulary, pinned so a rename in
    # campaign_monitor.py can't silently bypass the CHECK.
    expected = {
        "campaign_bounce_spike",
        "campaign_optout_spike",
        "campaign_no_engagement",
        "campaign_stalled",
        "campaign_quota_starved",
        "campaign_completed_summary",
    }
    assert expected <= set(MANAGER_ALERT_KINDS)
