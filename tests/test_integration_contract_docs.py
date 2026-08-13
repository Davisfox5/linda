"""Guards for the contracts external consoles code against.

``docs/ask-linda-integration.md`` is not decoration: the Flex console uses
its category table as an auto-apply allowlist, and its frame table to parse
the `/chat` stream. Both are the kind of document that rots silently — a new
recommendation category or a renamed SSE field breaks a consumer we cannot
see from this repo, and nothing here fails.

So these tests read the doc and compare it against the source. They are
deliberately shallow (regex over the module text, not import-time
introspection) because the thing being protected is a written contract, and
a shallow check that actually runs beats a precise one that doesn't.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "ask-linda-integration.md"
MANAGER = REPO / "backend" / "app" / "api" / "manager.py"
AGENT = REPO / "backend" / "app" / "services" / "linda_agent.py"
CHAT = REPO / "backend" / "app" / "api" / "chat.py"


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


# ── Manager recommendation categories ────────────────────────────────────


def _categories_handled_by_apply() -> set:
    """Every category string ``apply_recommendation`` dispatches on."""
    src = MANAGER.read_text(encoding="utf-8")
    start = src.index("async def apply_recommendation")
    end = src.index("rec.status = \"applied\"", start)
    body = src[start:end]

    found = set(re.findall(r'rec\.category == "([a-z_]+)"', body))
    # The `in (...)` tuple form used for the cohort-derived categories.
    for tup in re.findall(r"rec\.category in \(([^)]*)\)", body):
        found.update(re.findall(r'"([a-z_]+)"', tup))
    return found


def _categories_documented() -> set:
    """Every category named in the doc's full table."""
    table = _doc_text().split("### Full category table", 1)[1]
    return set(re.findall(r"^\| `([a-z_]+)` \|", table, flags=re.MULTILINE))


def test_every_handled_category_is_documented():
    """An undocumented category is one a consumer must guess about, and the
    safe guess (prospect-facing) is the one that blocks legitimate work."""
    undocumented = _categories_handled_by_apply() - _categories_documented()
    assert not undocumented, (
        f"categories handled in manager.py but missing from {DOC.name}: "
        f"{sorted(undocumented)}"
    )


def test_no_documented_category_has_been_removed_from_the_code():
    """The inverse drift: a doc promising a category the API now 400s on."""
    stale = _categories_documented() - _categories_handled_by_apply()
    assert not stale, (
        f"categories documented in {DOC.name} but no longer handled: "
        f"{sorted(stale)}"
    )


def test_run_campaign_is_the_only_category_flagged_prospect_facing():
    """The allowlist hinges on this single distinction. If a second
    category ever creates a Campaign, this test should fail and force the
    doc — and Flex's allowlist — to be updated deliberately."""
    src = MANAGER.read_text(encoding="utf-8")
    start = src.index("async def apply_recommendation")
    end = src.index("rec.status = \"applied\"", start)
    body = src[start:end]

    campaign_branches = re.findall(
        r'rec\.category == "([a-z_]+)"\s*:\s*\n\s*artifact = await _apply_run_campaign',
        body,
    )
    assert campaign_branches == ["run_campaign"], campaign_branches

    table = _doc_text().split("### Full category table", 1)[1]
    for line in table.splitlines():
        if not line.startswith("| `"):
            continue
        category = re.match(r"^\| `([a-z_]+)` \|", line).group(1)
        flagged = "Yes" in line.rsplit("|", 2)[1]
        assert flagged == (category == "run_campaign"), line


def test_apply_fails_closed_on_an_unknown_category():
    src = MANAGER.read_text(encoding="utf-8")
    assert 'detail=f"Unknown category: {rec.category}"' in src


# ── SSE frame names ──────────────────────────────────────────────────────

# The exact wire names external parsers bind to. Renaming any of these is a
# breaking change for every consumer, so it has to be a deliberate edit here
# and in the doc, not a drive-by rename.
EXPECTED_FRAMES = {
    "conversation",
    "text",
    "tool_use",
    "tool_result",
    "proposal",
    "error",
    "done",
}


def _frames_emitted() -> set:
    agent = AGENT.read_text(encoding="utf-8")
    chat = CHAT.read_text(encoding="utf-8")
    frames = set(re.findall(r'yield \{"type": "([a-z_]+)"', agent))
    frames.update(re.findall(r'yield _sse\(\s*\{\s*"type": "([a-z_]+)"', chat))
    frames.update(re.findall(r'_sse\(\{"type": "([a-z_]+)"', chat))
    return frames


def test_emitted_sse_frame_names_are_the_documented_ones():
    emitted = _frames_emitted()
    assert emitted <= EXPECTED_FRAMES, (
        f"undocumented SSE frame type(s): {sorted(emitted - EXPECTED_FRAMES)} — "
        f"add them to {DOC.name} before shipping"
    )
    # Every frame we promise should still be produced somewhere.
    assert EXPECTED_FRAMES <= emitted | {"conversation"}, sorted(
        EXPECTED_FRAMES - emitted
    )


def test_doc_lists_every_emitted_frame():
    doc = _doc_text()
    for frame in _frames_emitted():
        assert f"`{frame}`" in doc, f"{frame} missing from {DOC.name}"


def test_draft_tools_emit_proposal_instead_of_tool_result():
    """The single most misparseable thing about this stream — pinned so a
    refactor that unifies the two branches has to confront it."""
    agent = AGENT.read_text(encoding="utf-8")
    assert 'if block.name in DRAFT_TOOLS and "proposal_id" in result:' in agent
    assert 'yield {"type": "proposal", "proposal": result}' in agent


@pytest.mark.parametrize(
    "field",
    ['"type": "text", "delta"', '"type": "tool_use", "tool"', '"type": "tool_result", "tool"'],
)
def test_frame_field_names_are_unchanged(field):
    assert field in AGENT.read_text(encoding="utf-8")
