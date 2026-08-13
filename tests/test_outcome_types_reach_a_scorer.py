"""Every accepted outcome type should reach a scorer — or be declared dead.

``POST /outcomes`` accepts a fixed ``OutcomeType`` list, and
``DEFAULT_CALIBRATION_CONFIGS`` decides which of those keys actually become
calibration labels. Nothing connects the two, so a type can be accepted,
persisted, deduped, and then silently consumed by nobody. That failure is
invisible from both ends: the caller gets a 200 and the calibrator simply
never sees a pair.

That is not hypothetical. When this test was written, four accepted types
reached no scorer, while ``outcomes.py`` carried a comment asserting that
``DEFAULT_CALIBRATION_CONFIGS`` "consumes every key the calibrator can map
onto a positive/negative outcome". ``deal_won`` was one of them — which is
why wiring customer-level attribution into the calibrator would not, on its
own, have taught it anything about conversions.

This test does not force every type to be consumed. It forces the dead ones
to be **listed**, so adding one is a deliberate line in this file rather
than an accident nobody notices.
"""

import pytest

from backend.app.api.outcomes import OutcomeType
from backend.app.services.calibration import DEFAULT_CALIBRATION_CONFIGS

try:  # py3.9
    from typing import get_args
except ImportError:  # pragma: no cover
    from typing_extensions import get_args


# Accepted outcome types that intentionally reach no scorer today. Each needs
# a reason. Removing a key from here without wiring it up will fail the test
# below; adding one is a deliberate statement that the signal is inert.
KNOWN_UNCONSUMED = {
    "deal_won": (
        "No scorer maps it. The calibrated scorers are sentiment, churn_risk "
        "and upsell; a new-business win is none of those. Note churn_risk "
        "already treats deal_lost as a positive (churn) label, so the "
        "symmetric deal_won-as-negative is a live modelling decision, not an "
        "oversight to patch silently."
    ),
    "tenant_churned": (
        "churn_risk calibrates on contact_churned_30d, not tenant-level churn."
    ),
    "action_item_closed": "No scorer predicts action-item closure.",
    "action_item_closure_rate": "Same; also a rate, not a binary outcome.",
}


def _accepted() -> set:
    return set(get_args(OutcomeType))


def _consumed() -> set:
    keys = set()
    for cfg in DEFAULT_CALIBRATION_CONFIGS:
        keys.update(cfg.outcome_keys_positive)
        keys.update(cfg.outcome_keys_negative)
    return keys


def test_every_accepted_outcome_type_is_consumed_or_declared_dead():
    dead = _accepted() - _consumed() - set(KNOWN_UNCONSUMED)
    assert not dead, (
        f"outcome type(s) accepted by POST /outcomes but mapped by no scorer: "
        f"{sorted(dead)}. Either wire them into DEFAULT_CALIBRATION_CONFIGS or "
        f"add them to KNOWN_UNCONSUMED with a reason."
    )


def test_the_dead_list_does_not_go_stale():
    """If a listed type gets wired up, drop it from the list."""
    wired = set(KNOWN_UNCONSUMED) & _consumed()
    assert not wired, (
        f"{sorted(wired)} now reach a scorer — remove from KNOWN_UNCONSUMED"
    )


def test_the_dead_list_only_names_real_outcome_types():
    unknown = set(KNOWN_UNCONSUMED) - _accepted()
    assert not unknown, (
        f"KNOWN_UNCONSUMED names type(s) POST /outcomes does not accept: "
        f"{sorted(unknown)}"
    )


def test_no_scorer_consumes_a_key_the_api_cannot_accept():
    """The other direction: a scorer waiting on a key nothing can send is a
    label source that will never fire."""
    orphaned = _consumed() - _accepted()
    assert not orphaned, (
        f"scorer(s) calibrate on key(s) POST /outcomes cannot accept: "
        f"{sorted(orphaned)}"
    )


@pytest.mark.parametrize("outcome_type", sorted(KNOWN_UNCONSUMED))
def test_each_dead_type_carries_a_reason(outcome_type):
    assert KNOWN_UNCONSUMED[outcome_type].strip(), outcome_type
