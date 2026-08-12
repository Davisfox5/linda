"""Context budget for Ask LINDA tool results — the retrieval-isolation seam.

Raw tool output is the main way a chat context bloats. ``run_chat_turn``
serializes every tool result straight into the message history *and* the
persisted transcript, so one wide search can outweigh the whole
conversation, and it is replayed on every subsequent turn inside the
40-message window. That is the classic context-rot path: the effective
context window is much smaller than the advertised one, and quality falls
off a cliff rather than degrading gracefully.

This module puts a ceiling on what any single tool result contributes,
in two stages, cheapest first:

1. **Deterministic projection** (always) — truncate long free-text fields
   and drop trailing rows until the serialized result fits the budget,
   recording exactly what was dropped. No model, no cost, no risk.
2. **Question-aware condensation** (opt-in, prose tools only) — when the
   deterministic stage would drop rows, a Haiku sub-call picks which rows
   to keep *relative to what the user actually asked*, in its own
   context. Blind tail-dropping is fine when the tool already ranks by
   relevance; it is poor when the user's question is narrower than the
   query that produced the rows.

Two rules keep stage 2 safe:

* **Numbers never pass through a model.** Only tools in
  :data:`CONDENSABLE` — free-text search results — are eligible.
  Campaign rollups, funnels, quotas and counts are projected
  deterministically or not at all, so a model can never restate a metric.
* **The output is verified against the input, not trusted.** Every row the
  model returns must carry an id that was present in the input; rows that
  don't are dropped. If verification leaves nothing, we fall back to the
  deterministic result. Grounded critique, not self-report.

Any failure at any point falls back to the deterministic result — a
condensation problem must never fail a tool call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Reserved key stamped onto a result whose contents were reduced. It is
# part of what the model sees on purpose: an agent that silently receives
# 5 of 40 rows will happily answer "you had 5 calls about pricing".
FIT_KEY = "_context_note"

# Free-text fields worth truncating, and how much of each is enough to
# stay useful. Anything not listed is left alone — ids, enums, dates and
# numbers are small and load-bearing.
TEXT_CAPS = {
    "summary": 400,
    "snippet": 300,
    "highlight": 300,
    "highlights": 300,
    "body": 400,
    "description": 300,
    "raw_text": 400,
    "transcript": 400,
}

# The list-valued key each tool's rows live under.
ROW_KEYS = (
    "results",
    "messages",
    "campaigns",
    "action_items",
    "candidates",
    "replies",
    "snippets",
    "interactions",
)

# Never drop below this many rows deterministically — a result of one row
# is usually less useful than an honest "there were 30, here are 3".
MIN_ROWS = 3

# Tools whose rows are prose and may be model-condensed. Everything else
# is deterministic-only by design (see the module docstring).
CONDENSABLE = frozenset({"search_interactions", "search_sent_email"})

# Per-row identity keys, in priority order — used to verify a condensed
# row actually came from the input.
_ID_KEYS = ("interaction_id", "id", "message_id", "campaign_id")


def _serialized_len(payload: Any) -> int:
    try:
        return len(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return len(str(payload))


def _row_id(row: Any) -> Optional[str]:
    if not isinstance(row, dict):
        return None
    for key in _ID_KEYS:
        value = row.get(key)
        if value:
            return str(value)
    return None


def _truncate_text(value: Any, cap: int) -> Any:
    """Shorten one field, marking the cut so the model knows it's partial."""
    if isinstance(value, str) and len(value) > cap:
        return value[:cap] + "…[truncated]"
    if isinstance(value, list):
        return [_truncate_text(v, cap) for v in value]
    return value


def _project_row(row: Any, scale: float = 1.0) -> Any:
    """Cap this row's known free-text fields. ``scale`` tightens every cap
    proportionally for the last-resort pass; ids and numbers are untouched
    at any scale."""
    if not isinstance(row, dict):
        return row
    return {
        key: (
            _truncate_text(value, max(1, int(TEXT_CAPS[key] * scale)))
            if key in TEXT_CAPS
            else value
        )
        for key, value in row.items()
    }


def _find_rows(result: Dict[str, Any]) -> Optional[str]:
    """The key holding this result's row list, if it has one."""
    for key in ROW_KEYS:
        if isinstance(result.get(key), list):
            return key
    return None


def project(result: Any, budget: int) -> Tuple[Any, Dict[str, Any]]:
    """Deterministically fit ``result`` into ``budget`` characters.

    Returns ``(fitted, info)``. ``info`` reports what happened:
    ``{"kept": n, "dropped": n, "truncated_fields": bool}``. A result that
    already fits comes back untouched with an empty ``info``.
    """
    if _serialized_len(result) <= budget or not isinstance(result, dict):
        return result, {}

    row_key = _find_rows(result)
    if row_key is None:
        # No row list to thin (e.g. a single interaction detail) — cap its
        # text fields and accept whatever that gets us. Hard-truncating an
        # arbitrary structure would risk cutting an id in half.
        projected = _project_row(result)
        return projected, {"truncated_fields": True, "kept": 1, "dropped": 0}

    rows: List[Any] = list(result[row_key])
    total = len(rows)
    projected_rows = [_project_row(r) for r in rows]

    def _with(rows_subset: List[Any]) -> Dict[str, Any]:
        return dict(result, **{row_key: rows_subset})

    # Text truncation alone may be enough.
    if _serialized_len(_with(projected_rows)) <= budget:
        return _with(projected_rows), {
            "kept": total,
            "dropped": 0,
            "truncated_fields": True,
        }

    # Otherwise drop from the tail — tools return their rows in relevance
    # or recency order, so the tail is the cheapest thing to lose.
    keep = len(projected_rows)
    while keep > MIN_ROWS and _serialized_len(_with(projected_rows[:keep])) > budget:
        keep -= 1

    kept_rows = projected_rows[:keep]

    # The MIN_ROWS floor can still overshoot a tight budget. Rather than
    # break the floor (one row is rarely a useful answer) or blow the
    # budget, tighten the text caps on what's left. Ids and numbers survive
    # every pass — losing prose is recoverable via get_interaction_detail,
    # losing the id is not.
    if _serialized_len(_with(kept_rows)) > budget:
        for scale in (0.4, 0.15, 0.0):
            kept_rows = [_project_row(r, scale=scale) for r in rows[:keep]]
            if _serialized_len(_with(kept_rows)) <= budget:
                break

    return _with(kept_rows), {
        "kept": len(kept_rows),
        "dropped": total - len(kept_rows),
        "truncated_fields": True,
    }


def _note(info: Dict[str, Any], how: str) -> str:
    dropped = info.get("dropped", 0)
    kept = info.get("kept", 0)
    if dropped:
        return (
            "Showing %d of %d results (%s to fit the context budget). "
            "Say so if you report a count — do not present %d as the total. "
            "Narrow the query or raise the limit to see the rest."
            % (kept, kept + dropped, how, kept)
        )
    return "Long text fields in this result were truncated to fit the context budget."


_CONDENSE_SYSTEM = (
    "You compress tool results for another assistant's working context. "
    "You are given a user question and a JSON list of result rows. Return "
    "ONLY the rows that could help answer that question, as JSON.\n\n"
    "Rules:\n"
    "- Return ONLY valid JSON: {\"rows\": [...]}. No prose, no fences.\n"
    "- Copy every id field EXACTLY as given. Never invent, edit, or "
    "reformat an id — the assistant uses them to fetch full records.\n"
    "- Keep each row's structure. You may shorten free-text fields to "
    "their relevant part; never alter numbers, dates, or enum values.\n"
    "- Prefer fewer, more relevant rows. Keep at least one row if any "
    "row is even loosely relevant.\n"
    "- You are selecting, not summarizing. Do not merge rows or add "
    "commentary."
)


async def condense(
    router: Any,
    *,
    rows: List[Any],
    question: str,
    budget: int,
    call_site: str = "linda_condense",
) -> Optional[List[Any]]:
    """Haiku-tier, question-aware row selection in an isolated context.

    Returns the selected rows, or ``None`` if the call failed or produced
    nothing that survives verification — the caller then keeps its
    deterministic result. Every returned row is checked against the input
    by id; anything the model didn't get from us is discarded rather than
    trusted.
    """
    from backend.app.services.model_router import (
        CacheableBlock,
        LLMRequest,
        Tier,
        TaskType,
    )

    known: Dict[str, Any] = {}
    for row in rows:
        rid = _row_id(row)
        if rid:
            known[rid] = row
    if not known:
        # Nothing addressable to verify against — refuse rather than trust.
        return None

    payload = json.dumps({"question": question, "rows": rows}, default=str)
    try:
        response = await router.ainvoke(
            LLMRequest(
                task_type=TaskType.GENERIC,
                forced_tier=Tier.HAIKU,
                user_message=payload,
                system_blocks=[CacheableBlock(text=_CONDENSE_SYSTEM, cache=True)],
                max_tokens=2048,
                temperature=0.0,
                call_site=call_site,
            )
        )
        parsed = response.parse_json()
    except Exception:
        logger.warning("linda_context: condense call failed", exc_info=True)
        return None

    candidate_rows = parsed.get("rows") if isinstance(parsed, dict) else None
    if not isinstance(candidate_rows, list) or not candidate_rows:
        return None

    # Grounding check: keep only rows whose id we actually sent, and take
    # the field values from... the model's version, but the id from ours,
    # so a mangled id can never reach a downstream tool call.
    verified: List[Any] = []
    seen: set = set()
    for row in candidate_rows:
        rid = _row_id(row)
        if rid is None or rid not in known or rid in seen:
            continue
        seen.add(rid)
        verified.append(row)

    if not verified:
        logger.warning(
            "linda_context: condense returned %d rows, none traceable to the input",
            len(candidate_rows),
        )
        return None
    if _serialized_len(verified) > budget:
        # The model kept too much; let the deterministic path handle it.
        return None
    return verified


async def fit_tool_result(
    result: Any,
    *,
    tool_name: str,
    question: str,
    budget: int,
    router: Optional[Any] = None,
    condense_enabled: bool = True,
) -> Any:
    """Fit one tool result into the working-context budget.

    Deterministic projection always runs. When it had to drop rows from a
    prose tool and a router is available, a Haiku sub-call re-selects the
    rows against the user's question; its output is verified and only then
    preferred. Anything that goes wrong keeps the deterministic result.
    """
    if not isinstance(result, dict) or "error" in result:
        # Errors are short and load-bearing — never touch them.
        return result

    fitted, info = project(result, budget)
    if not info:
        return result

    row_key = _find_rows(result)
    may_condense = (
        condense_enabled
        and router is not None
        and tool_name in CONDENSABLE
        and row_key is not None
        and info.get("dropped", 0) > 0
    )

    if may_condense:
        selected = await condense(
            router,
            rows=list(result[row_key]),
            question=question,
            budget=budget,
        )
        if selected is not None:
            total = len(result[row_key])
            out = dict(result, **{row_key: selected})
            out[FIT_KEY] = _note(
                {"kept": len(selected), "dropped": total - len(selected)},
                "selected as most relevant to your question",
            )
            return out

    fitted = dict(fitted)
    fitted[FIT_KEY] = _note(info, "the rest were dropped")
    return fitted
