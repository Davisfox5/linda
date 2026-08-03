"""Per-prospect cold-outreach draft personalization.

Sonnet via ModelRouter (same tier + voice as the follow-up drafts in
api/emails.py — outbound prose quality is the bar; Haiku reads flat).
Runs sync (Celery). One call per prospect: volume is bounded by the
campaign daily throttle (~25/day), so per-call latency dominates cost
and the Batches API isn't worth the indirection yet.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from backend.app.models import Campaign, Customer, OutreachMember
from backend.app.services.model_router import (
    CacheableBlock,
    LLMRequest,
    TaskType,
    Tier,
    get_router,
)
from backend.app.services.outreach.common import (
    OutreachConfig,
    render_placeholders,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You personalize ONE cold-outreach email for a B2B sale.

You are writing on behalf of {sender_name} at {sender_business}. The
recipient is a small-business owner. Start from the template, then adapt
it with the prospect facts — especially the "hook" (why we win against
their current tooling). Rules:

- Keep it under 140 words. Plain text. No links unless the template has one.
- The template may use **bold**, *italic*, _underline_, or [label](url)
  link markers. Keep them on the same phrases where they survive your
  edit — a link's label and url must both pass through verbatim — and
  never add formatting or links the template doesn't have.
- Sound like a person writing one email, not a mail merge. Use at most
  ONE prospect-specific detail naturally; don't enumerate their data back
  at them.
- Never invent facts, pricing, or claims not present in the template or
  prospect data. Never promise anything the template doesn't.
- Do not add an unsubscribe/footer line — the system appends the
  compliance footer separately.
- Subject: short, specific, no clickbait, no ALL CAPS, no "RE:" tricks.

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""


def _prospect_facts(customer: Customer) -> dict:
    outreach_meta = (customer.metadata_ or {}).get("outreach", {})
    return {
        "business_name": customer.name,
        "city": outreach_meta.get("city"),
        "state": outreach_meta.get("state"),
        "segment": outreach_meta.get("segment"),
        "current_software": outreach_meta.get("current_software"),
        "hook": outreach_meta.get("hook"),
        "website": customer.domain,
    }


def generate_member_draft(
    campaign: Campaign,
    config: OutreachConfig,
    member: OutreachMember,
    customer: Customer,
    step_index: Optional[int] = None,
) -> dict:
    """Generate {subject, body} for the member's current (or given) step.

    Raises on LLM/parse failure — callers decide whether that fails the
    member or retries later.
    """
    step_idx = member.current_step if step_index is None else step_index
    steps = config.steps
    step = steps[min(step_idx, len(steps) - 1)]
    facts = _prospect_facts(customer)

    base_subject = render_placeholders(config.template.subject, facts)
    base_body = render_placeholders(config.template.body, facts)

    parts = [
        "## Template subject\n" + base_subject,
        "## Template body\n" + base_body,
        "## Prospect facts\n"
        + json.dumps({k: v for k, v in facts.items() if v}, ensure_ascii=False),
    ]
    if step_idx > 0:
        parts.append(
            "## This is follow-up touch #{n} (no reply so far)\n"
            "Write a SHORT bump (2-4 sentences) that threads on the "
            "original: reference it lightly, add one new angle from the "
            "hook if available, and make the ask smaller (e.g. a yes/no "
            "question). Do not repeat the original pitch.".format(n=step_idx + 1)
        )
    if step.guidance:
        parts.append("## Step guidance\n" + step.guidance)
    parts.append('Write the email now. Return ONLY {"subject": ..., "body": ...}.')

    system = _SYSTEM_PROMPT.format(
        sender_name=config.template.sender_name,
        sender_business=config.template.sender_business,
    )

    response = get_router().invoke(
        LLMRequest(
            task_type=TaskType.GENERIC,
            forced_tier=Tier.SONNET,
            user_message="\n\n".join(parts),
            # The system prompt is identical across every member of the
            # campaign → prompt-cache it for the fan-out.
            system_blocks=[CacheableBlock(text=system, cache=True)],
            max_tokens=1024,
            temperature=0.3,
            call_site="outreach_draft",
        )
    )
    parsed = response.parse_json()
    subject = str(parsed.get("subject") or base_subject).strip()[:400]
    body = str(parsed.get("body") or "").strip()
    if not body:
        raise ValueError("outreach draft came back without a body")
    return {"subject": subject, "body": body, "facts": facts}
