"""Tests for the Ask LINDA tool-result context budget (linda_context).

The seam has two stages — deterministic projection, then optional
question-aware Haiku condensation — and the safety properties that matter
are mostly about what it must NOT do: never touch numbers, never trust a
model-returned id, never fail a tool call.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import linda_context
from backend.app.services.linda_context import (
    FIT_KEY,
    fit_tool_result,
    project,
)


def _search_result(n, summary_len=600):
    return {
        "results": [
            {
                "interaction_id": "11111111-1111-1111-1111-%012d" % i,
                "score": 0.9 - i / 100.0,
                "summary": "s" * summary_len,
                "highlights": ["h" * summary_len],
                "channel": "voice",
                "created_at": "2026-08-0%dT00:00:00+00:00" % ((i % 9) + 1),
            }
            for i in range(n)
        ]
    }


def _fit(result, **over):
    kwargs = {
        "tool_name": "search_interactions",
        "question": "what did Acme say about pricing?",
        "budget": 2000,
        "router": None,
        "condense_enabled": False,
    }
    kwargs.update(over)
    return asyncio.run(fit_tool_result(result, **kwargs))


# ── Deterministic projection ───────────────────────────────────────────────


def test_small_result_passes_through_untouched():
    """The common case must cost nothing and lose nothing."""
    small = {"results": [{"interaction_id": "abc", "summary": "short"}]}
    assert _fit(small) == small
    assert FIT_KEY not in _fit(small)


def test_oversized_result_is_brought_under_budget():
    """The budget is a real ceiling, not a target: the row floor yields to
    it by shedding prose rather than by overshooting."""
    fitted = _fit(_search_result(40))
    payload = dict(fitted)
    note = payload.pop(FIT_KEY)
    assert len(json.dumps(payload)) <= 2000
    assert note  # the reduction is always disclosed


def test_budget_holds_even_when_the_row_floor_cannot_fit():
    """A budget too small for MIN_ROWS of full rows still holds — ids and
    numbers survive, prose is what gets sacrificed."""
    fitted = _fit(_search_result(40, summary_len=9000), budget=700)
    payload = dict(fitted)
    payload.pop(FIT_KEY)
    assert len(json.dumps(payload)) <= 700
    assert len(fitted["results"]) == linda_context.MIN_ROWS
    for row in fitted["results"]:
        assert len(row["interaction_id"]) == 36
        assert row["score"] is not None


def test_truncation_alone_is_preferred_over_dropping_rows():
    """Long text is cheap to shorten; rows are expensive to lose. A result
    that fits once its prose is capped must keep every row."""
    result = _search_result(4, summary_len=5000)
    fitted = _fit(result, budget=6000)
    assert len(fitted["results"]) == 4
    assert fitted["results"][0]["summary"].endswith("[truncated]")


def test_dropped_rows_are_disclosed_with_the_real_total():
    """An agent handed 3 of 40 rows will otherwise report "you had 3 calls"."""
    fitted = _fit(_search_result(40))
    note = fitted[FIT_KEY]
    assert "of 40" in note
    assert str(len(fitted["results"])) in note
    assert "do not present" in note.lower()


def test_ids_are_never_truncated():
    """Ids are the chaining key — a half-truncated id is worse than a
    dropped row, because the model will confidently call a tool with it."""
    fitted = _fit(_search_result(40))
    for row in fitted["results"]:
        assert len(row["interaction_id"]) == 36
        assert "truncated" not in row["interaction_id"]


def test_projection_keeps_a_floor_of_rows():
    result = _search_result(40, summary_len=4000)
    fitted, info = project(result, budget=200)
    assert len(fitted["results"]) == linda_context.MIN_ROWS
    assert info["dropped"] == 40 - linda_context.MIN_ROWS


def test_error_results_are_never_reshaped():
    err = {"error": "search failed: boom"}
    assert _fit(err) == err


def test_structure_without_a_row_list_is_capped_not_chopped():
    """A single-record result (interaction detail) has no rows to drop, so
    it gets field capping — never a blind string truncation that could cut
    an id in half."""
    detail = {
        "interaction": {
            "id": "22222222-2222-2222-2222-222222222222",
            "summary": "x" * 9000,
            "sentiment_score": 0.42,
        }
    }
    fitted = _fit(detail, budget=1000)
    assert fitted["interaction"]["id"] == "22222222-2222-2222-2222-222222222222"
    assert fitted["interaction"]["sentiment_score"] == 0.42


# ── The safety rules on stage 2 ────────────────────────────────────────────


def test_numeric_tools_are_never_model_condensed():
    """Campaign rollups/funnels/quotas must not pass through a model —
    a restated metric is indistinguishable from a real one downstream."""
    for tool in ("get_campaign_stats", "list_campaign_replies", "list_campaigns",
                 "get_action_items", "resolve_entity", "get_interaction_detail"):
        assert tool not in linda_context.CONDENSABLE
    assert linda_context.CONDENSABLE == {"search_interactions", "search_sent_email"}


def test_condense_is_skipped_when_projection_dropped_nothing():
    """No rows lost => no judgment call to make => don't pay for a model."""
    router = SimpleNamespace(ainvoke=AsyncMock())
    _fit(_search_result(4, summary_len=5000), budget=6000, router=router,
         condense_enabled=True)
    router.ainvoke.assert_not_awaited()


def _router_returning(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=SimpleNamespace(
                text=text,
                parse_json=lambda: json.loads(text),
            )
        )
    )


def test_condensed_rows_are_used_when_they_verify():
    keep = _search_result(40)["results"][:2]
    for row in keep:
        row["summary"] = "trimmed to the pricing part"
    router = _router_returning({"rows": keep})

    fitted = _fit(_search_result(40), router=router, condense_enabled=True)

    assert [r["interaction_id"] for r in fitted["results"]] == [
        r["interaction_id"] for r in keep
    ]
    assert "most relevant" in fitted[FIT_KEY]
    assert "of 40" in fitted[FIT_KEY]


def test_hallucinated_ids_are_dropped_not_trusted():
    """The grounding check: a row whose id we never sent cannot reach the
    main context, because the agent would then call a tool with it."""
    invented = {
        "interaction_id": "99999999-9999-9999-9999-999999999999",
        "summary": "a call that does not exist",
    }
    router = _router_returning({"rows": [invented]})

    fitted = _fit(_search_result(40), router=router, condense_enabled=True)

    ids = {r["interaction_id"] for r in fitted["results"]}
    assert "99999999-9999-9999-9999-999999999999" not in ids
    # Verification emptied the selection, so the deterministic result stands.
    assert "dropped" in fitted[FIT_KEY] or "of 40" in fitted[FIT_KEY]


def test_partially_hallucinated_selection_keeps_only_the_real_rows():
    real = _search_result(40)["results"][3]
    router = _router_returning({
        "rows": [
            real,
            {"interaction_id": "99999999-9999-9999-9999-999999999999", "summary": "nope"},
        ]
    })

    fitted = _fit(_search_result(40), router=router, condense_enabled=True)

    assert [r["interaction_id"] for r in fitted["results"]] == [real["interaction_id"]]


def test_condense_failure_falls_back_to_deterministic_result():
    """A provider blip must degrade the context, never fail the tool."""
    router = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("overloaded")))

    fitted = _fit(_search_result(40), router=router, condense_enabled=True)

    assert fitted["results"]
    assert FIT_KEY in fitted
    router.ainvoke.assert_awaited()


def test_condense_returning_junk_falls_back():
    for junk in ("not json at all", {"rows": "not a list"}, {"rows": []}, {}):
        router = _router_returning(junk) if not isinstance(junk, str) else SimpleNamespace(
            ainvoke=AsyncMock(
                return_value=SimpleNamespace(
                    text=junk,
                    parse_json=lambda: (_ for _ in ()).throw(ValueError("bad json")),
                )
            )
        )
        fitted = _fit(_search_result(40), router=router, condense_enabled=True)
        assert fitted["results"], junk


def test_condense_disabled_by_config_never_calls_the_model():
    router = SimpleNamespace(ainvoke=AsyncMock())
    _fit(_search_result(40), router=router, condense_enabled=False)
    router.ainvoke.assert_not_awaited()


def test_condense_runs_on_haiku_in_its_own_context():
    """Context isolation: the sub-call gets the rows and the question, not
    the conversation — and it runs on the cheap tier."""
    from backend.app.services.model_router import Tier

    keep = _search_result(40)["results"][:2]
    router = _router_returning({"rows": keep})
    _fit(_search_result(40), router=router, condense_enabled=True)

    req = router.ainvoke.call_args.args[0]
    assert req.forced_tier == Tier.HAIKU
    assert req.temperature == 0.0
    assert req.call_site == "linda_condense"
    payload = json.loads(req.user_message)
    assert payload["question"] == "what did Acme say about pricing?"
    assert len(payload["rows"]) == 40


@pytest.mark.asyncio
async def test_condense_refuses_rows_without_ids():
    """Nothing addressable to verify against => don't run the model at all,
    because its output could not be checked."""
    router = SimpleNamespace(ainvoke=AsyncMock())
    out = await linda_context.condense(
        router, rows=[{"summary": "no id here"}], question="q", budget=2000
    )
    assert out is None
    router.ainvoke.assert_not_awaited()
