---
description: Overlapping review (L3) — code-reviewer and security-reviewer run siloed on the same diff
argument-hint: [git ref, defaults to the working tree vs main]
---

Run the **overlapping review loop (L3)** from `docs/agent-orchestration-map.md`.

Target: $ARGUMENTS (default: the working tree diff against `main`).

**Run both reviewers in the SAME message so they run concurrently, and give neither
one the other's findings.** The siloing is the point: a correctness lens and a
tenant-isolation lens find different bug classes, and letting the first anchor the
second wastes the second.

1. `code-reviewer` (fable) — conventions, correctness, Python 3.9 typing floor, the
   model-catalog seam, Alembic migration safety under Fly's release-command deploy,
   test coverage for new behavior.
2. `security-reviewer` (fable) — RLS/tenant isolation, auth scopes, unauthenticated
   surfaces, Stripe webhook verification and replay, Fernet/dev-key fallback,
   injection, prompt-injection paths, dependency risk against the pins.

Then, in this session:
- **Merge and dedup.** Same `file:line` + same mechanism = one finding. Keep the
  sharper failure scenario.
- **Rank by severity**, and for each finding state: act now, defer with a reason, or
  disagree with a reason. A finding you silently drop is a finding you accepted.
- **Resolve disagreements here.** The reviewers do not argue with each other — that
  would be an ungrounded loop. You adjudicate, citing the code.

**Mandatory trigger.** Use this instead of a lone `code-reviewer` whenever the diff
touches a path in `.claude/sensitive-paths.md`, adds an API endpoint or webhook
surface, adds a table or a migration, or changes an LLM-spending path. Otherwise a
single `code-reviewer` is enough — this is two fable calls and is gated on purpose.

Route the surviving findings back through the delivery loop (L1) at
`code-writer`, respecting its 2-round bound.
