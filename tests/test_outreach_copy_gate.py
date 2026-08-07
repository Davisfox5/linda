"""Unit tests for the send-time copy gate (services/outreach/copy_gate.py).

Ports the case list from the JS source of truth's test suite
(``src/lib/outreach/__tests__/copy-gate.test.ts`` in the Flex repo) so
the two implementations can't drift apart on what passes, plus a few
Python-side extras for checks the JS suite leaves implicit.
"""

from __future__ import annotations

from backend.app.services.outreach.copy_gate import evaluate_draft

# The gate keys on the valediction line; the domains below exercise the
# signature link exemption.
SIGNATURE = """All the best,

Pat Doe, CSCS
Owner & Founder
Example Gym (gym.example.com)
Example Platform (platform.example.net)
Book a 15-minute demo: platform.example.net/book"""


def send_ready_draft(biz: str) -> str:
    return f"""Hi {biz},

This is Davison Fox, gym owner and CSCS. I built a software called Flex \
to run my own strength and conditioning gym.

Most gym owners are paying for four separate tools: a website, a \
scheduling app, a payment processor, and a workout tracker. Flex does \
all four from $59/mo, month to month, and you get paid directly. We \
never hold your money.

I came across {biz} and saw you cap your small groups at four people. \
You're exactly who Flex was built for.

Would Flex be worth a look? I'm happy to show you what it would look \
like for {biz}.

{SIGNATURE}"""


# ── Ported from the JS suite ───────────────────────────────────────────


def test_passes_the_approved_outreach_template():
    result = evaluate_draft(send_ready_draft("Acme"), "Acme")
    assert result["failures"] == []
    assert result["pass"] is True


def test_normalizes_business_name_so_long_names_cannot_skew_fk_band():
    # Also covers the banned-phrase interplay: "Synergy" in the prospect's
    # own name must not trip the filler/spam check once normalized.
    biz = "Synergy Performance & Rehabilitation Institute"
    assert evaluate_draft(send_ready_draft(biz), biz)["pass"] is True


def test_fails_on_an_em_dash_anywhere_in_the_body():
    draft = send_ready_draft("Acme").replace(
        "month to month, and", "month to month — and"
    )
    result = evaluate_draft(draft, "Acme")
    assert result["pass"] is False
    assert any("em/en dashes" in f for f in result["failures"])


def test_exempts_signature_block_from_prose_metrics_and_link_budget():
    # Signature carries three domains; the body has zero links, so the
    # <=1 body link check must still pass.
    result = evaluate_draft(send_ready_draft("Acme"), "Acme")
    assert not any("body link" in f for f in result["failures"])


def test_fails_on_consecutive_choppy_sentences_within_a_paragraph():
    draft = send_ready_draft("Acme").replace(
        "Would Flex be worth a look? I'm happy to show you what it would look "
        "like for Acme.",
        "Worth a look? Just say so. I will send it over right away for you and "
        "your team.",
    )
    result = evaluate_draft(draft, "Acme")
    assert result["pass"] is False
    assert any("choppiness" in f for f in result["failures"])


def test_fails_on_dropped_subject_verb_openers():
    draft = send_ready_draft("Acme").replace("I came across Acme and saw", "Saw")
    result = evaluate_draft(draft, "Acme")
    assert result["pass"] is False
    assert any("dropped-subject" in f for f in result["failures"])


def test_fails_on_banned_filler_phrases():
    draft = send_ready_draft("Acme").replace(
        "This is Davison Fox, gym owner and CSCS.",
        "This is Davison Fox, just following up here.",
    )
    result = evaluate_draft(draft, "Acme")
    assert result["pass"] is False
    assert any("filler/spam" in f for f in result["failures"])


# ── Python-side extras ─────────────────────────────────────────────────


def test_fails_short_draft_on_word_count_and_questions():
    result = evaluate_draft("Hi Acme,\n\nQuick note. No ask here.\n\n" + SIGNATURE, "Acme")
    assert result["pass"] is False
    assert any("word count 50-110" in f for f in result["failures"])
    assert any("1-3 questions" in f for f in result["failures"])


def test_fails_on_second_body_link():
    draft = send_ready_draft("Acme").replace(
        "I came across Acme and saw you cap your small groups at four people.",
        "I came across Acme on gymlist.com/directory and again on "
        "flexonline.net/demo just yesterday.",
    )
    result = evaluate_draft(draft, "Acme")
    assert result["pass"] is False
    assert any("body link" in f for f in result["failures"])


def test_skips_standalone_greeting_from_sentence_metrics():
    # The greeting "Hi Acme," (2 words) would otherwise register as a
    # choppy short sentence and drag the average under the 8-word floor.
    result = evaluate_draft(send_ready_draft("Acme"), "Acme")
    assert 8 <= result["metrics"]["avg_sentence"] <= 16
    assert result["metrics"]["choppy_pairs"] == []
