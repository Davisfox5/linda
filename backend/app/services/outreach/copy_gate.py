"""Send-time copy-quality gate for outreach emails.

Python port of the house copy checks — the JS source of truth is
``src/lib/outreach/copy-gate.mjs`` in the Flex repo, scored against the
evidence-based targets in its outreach-effectiveness research §11. The
two implementations must stay behaviorally identical: an email that the
Flex CLI/approval routes would approve must pass here, and vice versa
(``tests/test_outreach_copy_gate.py`` ports the JS test cases to hold
the line).

Targets: words 50-110 | FK grade 3.5-4.5 (house hard band) | avg
sentence 8-16 words | no two consecutive sub-7-word sentences in a
paragraph (choppiness) | no em/en dashes | no dropped-subject verb
openers ("Saw..." for "I saw...") | 1-3 questions | you:I >= 0.8 |
<= 1 body link | no filler/spam phrases. The signature block
(valediction down) is exempt from prose metrics and the link budget; a
standalone greeting line ("Hi {business_name},") is exempt from
sentence metrics.

Enforced on prospect-facing campaign sends only (draft generation in
auto mode + the send-time backstop in the scheduler), never on
transactional mail. ``settings.OUTREACH_COPY_GATE_ENABLED`` is the
emergency off-switch.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

BANNED = [
    "just checking in", "touching base", "circling back", "just following up",
    "hope this email finds you well", "hope this finds you well",
    "to whom it may concern", "my name is", "i wanted to reach out",
    "synergy", "leverage", "revolutionary", "game-changing", "cutting-edge",
    "act now", "limited time", "guaranteed", "risk-free", "100% free",
]

# Sentence-initial bare verbs that read as a dropped "I" ("Saw your site...").
_DROPPED_SUBJECT_RE = re.compile(
    r"(?:^|[.!?]\s+)"
    r"(Saw|Came|Noticed|Wanted|Figured|Thought|Loved|Got|Ran|Reached|Checked|Looked)\b",
    re.MULTILINE,
)

_SIGNATURE_RE = re.compile(
    r"^(All the best|Best|Cheers|Thanks|Talk soon),?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_WORD_RE = re.compile(r"[A-Za-z'{}$0-9-]+")


def _syllables(word: str) -> int:
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    stripped = re.sub(r"[^laeiouy]e$", "", word)
    stripped = re.sub(r"^y", "", stripped)
    groups = re.findall(r"[aeiouy]{1,2}", stripped)
    return max(1, len(groups))


def _count_words(s: str) -> int:
    return len(_WORD_RE.findall(s))


def analyze(raw: str, business_name: Optional[str] = None) -> Dict[str, Any]:
    if business_name:
        # Proper nouns are fixed content; without normalizing them a long
        # gym name would mathematically push any email out of the FK band.
        raw = re.sub(re.escape(business_name), "Acme", raw, flags=re.IGNORECASE)
    # Signature block (from the valediction down) is excluded from prose
    # metrics and the link count: signature domain/booking links are
    # identity, not CTAs.
    sig = _SIGNATURE_RE.search(raw)
    if sig:
        body_only = raw[: sig.start()]
    else:
        body_only = re.sub(r"^[—-]\s.*$", "", raw, count=1, flags=re.MULTILINE)
    em_dashes = len(re.findall(r"[—–]", body_only))
    dropped_subjects = [m.group(1) for m in _DROPPED_SUBJECT_RE.finditer(body_only)]

    # Strip placeholders and links so metrics reflect prose.
    text = re.sub(r"\{[a-z_ /]+\}", "Acme", body_only, flags=re.IGNORECASE)
    text = re.sub(
        r"https?://\S+|[a-z0-9.-]+\.(?:net|com|io|fit)/\S*",
        "LINK",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip()

    # Paragraph-aware sentence analysis. A standalone greeting ("Hi Acme,")
    # is its own paragraph and is skipped.
    paras = [p.strip() for p in re.split(r"\n{2,}", text)]
    paras = [p for p in paras if re.search(r"[a-z]", p, re.IGNORECASE)]
    paras = [
        p
        for p in paras
        if not (
            re.match(r"^(hi|hey|hello)\b[^.!?]*,?$", p, re.IGNORECASE)
            and _count_words(p) <= 5
        )
    ]

    sentences: List[str] = []
    choppy_pairs: List[str] = []
    for p in paras:
        ss = [s.strip() for s in re.split(r"(?<=[.!?])\s+", p)]
        ss = [s for s in ss if re.search(r"[a-z]", s, re.IGNORECASE)]
        for k, s in enumerate(ss):
            sentences.append(s)
            if k > 0 and _count_words(s) < 7 and _count_words(ss[k - 1]) < 7:
                choppy_pairs.append('"{0} {1}"'.format(ss[k - 1], s))

    words = [w for s in sentences for w in _WORD_RE.findall(s)]
    sylls = sum(_syllables(w) for w in words)
    questions = body_only.count("?")
    links = len(
        re.findall(
            r"https?://|(?:^|\s)[a-z0-9-]+\.(?:net|com|io|fit)/",
            body_only,
            re.IGNORECASE,
        )
    )
    you = len(re.findall(r"\byou(?:r|rs)?\b", body_only, re.IGNORECASE))
    # Case-sensitive on purpose (mirrors the JS): "I" is always capitalized.
    i = len(re.findall(r"\bI(?:'m|'ve|'d|'ll)?\b|\bmy\b|\bme\b", body_only))
    grade = (
        0.39 * (len(words) / max(1, len(sentences)))
        + 11.8 * (sylls / max(1, len(words)))
        - 15.59
    )
    avg_sentence = len(words) / max(1, len(sentences))
    banned = [p for p in BANNED if p in raw.lower()]

    return {
        "words": len(words),
        "sentences": len(sentences),
        "avg_sentence": avg_sentence,
        "grade": grade,
        "questions": questions,
        "links": links,
        "you": you,
        "i": i,
        "banned": banned,
        "em_dashes": em_dashes,
        "choppy_pairs": choppy_pairs,
        "dropped_subjects": dropped_subjects,
    }


def evaluate_draft(raw: str, business_name: Optional[str] = None) -> Dict[str, Any]:
    """Run every house check. Returns the full check list plus the
    failures alone, as human-readable strings — the shape both the review
    inbox and ``member.personalization["copy_gate_failures"]`` store."""
    a = analyze(raw, business_name)
    checks = [
        ("word count 50-110", 50 <= a["words"] <= 110, str(a["words"])),
        ("FK grade 3.5-4.5 (house hard band)", 3.5 <= a["grade"] <= 4.5, "%.1f" % a["grade"]),
        ("avg sentence 8-16 words", 8 <= a["avg_sentence"] <= 16, "%.1f" % a["avg_sentence"]),
        (
            "no consecutive sub-7-word sentences (choppiness)",
            not a["choppy_pairs"],
            a["choppy_pairs"][0] if a["choppy_pairs"] else "none",
        ),
        ("no em/en dashes (hard rule)", a["em_dashes"] == 0, str(a["em_dashes"])),
        (
            "no dropped-subject verb openers ('Saw...' for 'I saw...')",
            not a["dropped_subjects"],
            ", ".join(a["dropped_subjects"]) or "none",
        ),
        ("1-3 questions", 1 <= a["questions"] <= 3, str(a["questions"])),
        (
            "you:I ratio >= 0.8",
            a["i"] == 0 or a["you"] / a["i"] >= 0.8,
            "{0}:{1}".format(a["you"], a["i"]),
        ),
        ("<= 1 body link (signature exempt)", a["links"] <= 1, str(a["links"])),
        ("no filler/spam phrases", not a["banned"], ", ".join(a["banned"]) or "clean"),
    ]
    failures = [
        "{0} ({1})".format(label, val) for label, ok, val in checks if not ok
    ]
    return {"pass": not failures, "failures": failures, "checks": checks, "metrics": a}
