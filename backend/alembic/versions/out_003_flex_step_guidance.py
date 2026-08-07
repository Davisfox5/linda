"""Data repair: rewrite the July Flex campaign's follow-up step guidance.

Campaign ``3decfab7-8435-4432-b18d-70466868fecc`` (the Flex tenant's
July cold-outreach sequence) is live with follow-up steps whose guidance
predates the house copy gate (services/outreach/copy_gate.py). Replace
the guidance on steps 2-4 — matched by their offsets (+14/+28/+42) —
with the gate-aligned prompts (short bump / value-add touch / soft
breakup), each carrying the shared hard-rules style block. Step 1
(offset 0) is untouched; members already past a step simply draft their
NEXT touch from the new guidance.

Same pattern as prior data repairs (w0e1f2a3b4c5): forward-only, no-op
on databases that don't hold the campaign, and prints the before/after
guidance heads into the release logs for verification.

Revision ID: out_003_flex_step_guidance
Revises: sen_001_schema_drift
Create Date: 2026-08-07
"""
from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "out_003_flex_step_guidance"
down_revision: Union[str, None] = "sen_001_schema_drift"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CAMPAIGN_ID = "3decfab7-8435-4432-b18d-70466868fecc"

_STYLE = (
    "Style, hard rules: NEVER use an em dash or en dash anywhere, use a comma "
    "or start a new sentence instead; never start a sentence with a bare verb "
    "that drops the subject, always keep the pronoun (write 'I saw' or 'I came "
    "across', never 'Saw' or 'Came across'); Flesch-Kincaid grade 3.5-4.5, the "
    "house hard band, with sentences that flow on commas rather than short "
    "punchy fragments; say 'you' more than 'I'; slightly warm, no gushing; "
    "never 'just checking in', 'touching base', 'circling back', 'just "
    "following up', or 'hope this finds you well'. Sign off exactly: 'All the "
    "best,' then 'Davison Fox, CSCS' / 'Owner & Founder' / 'First XI Fitness "
    "(firstxifitness.com)' / 'Flex Online (flexonline.net)' / 'Book a "
    "15-minute demo: flexonline.net/book'."
)

GUIDANCE_BY_OFFSET = {
    14: (
        "Short bump in the same thread, under 50 words of body. Reference the "
        "earlier email in one natural clause, add ONE new concrete Flex fact "
        "(for example, clients follow their programs in any browser with no "
        "app to download). Soft interest CTA, exactly one question, no body "
        "links. {STYLE}"
    ).replace("{STYLE}", _STYLE),
    28: (
        "Value-add touch, 60-90 words of body. Share one genuinely useful "
        "pointer for a gym owner comparing software, and mention the live "
        "demo on flexonline.net as the only body link, one click and no "
        "signup needed. No meeting ask; the CTA is one soft question. {STYLE}"
    ).replace("{STYLE}", _STYLE),
    42: (
        "Soft breakup, 60-80 words of body. Say you'll stop emailing and "
        "leave the door open (flexonline.net, 30-day free trial, no card "
        "needed). Exactly one question. End with one genuine compliment tied "
        "to what you know about this business. No body links other than "
        "flexonline.net. {STYLE}"
    ).replace("{STYLE}", _STYLE),
}


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        text("SELECT config FROM campaigns WHERE id = CAST(:id AS uuid)"),
        {"id": CAMPAIGN_ID},
    ).first()
    if row is None:
        print(f"out_003: campaign {CAMPAIGN_ID} not in this database; nothing to do")
        return

    config = dict(row[0] or {})
    steps = [dict(s) for s in (config.get("steps") or [])]
    changed = 0
    for idx, step in enumerate(steps):
        if idx == 0:
            continue  # first touch keeps its guidance
        offset = step.get("offset_days")
        new_guidance = GUIDANCE_BY_OFFSET.get(offset)
        if new_guidance is None:
            print(
                f"out_003: step[{idx}] offset_days={offset!r} has no replacement; skipping"
            )
            continue
        before = step.get("guidance") or ""
        step["guidance"] = new_guidance
        changed += 1
        print(
            f"out_003: step[{idx}] (+{offset}d)\n"
            f"  before: {before[:100]!r}\n"
            f"  after:  {new_guidance[:100]!r}"
        )
    if not changed:
        print("out_003: no matching steps found; config left untouched")
        return

    config["steps"] = steps
    bind.execute(
        text(
            "UPDATE campaigns SET config = CAST(:cfg AS jsonb) "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"cfg": json.dumps(config), "id": CAMPAIGN_ID},
    )
    print(f"out_003: updated {changed} step(s) on campaign {CAMPAIGN_ID}")


def downgrade() -> None:
    # Forward-only data repair: the prior guidance is superseded, not
    # worth resurrecting (it's what the copy gate now rejects).
    pass
