---
description: Delivery loop (L1) — plan → spec → implement → verify → review, bounded at 2 remediation rounds
argument-hint: <what to build>
---

Run the **delivery loop (L1)** from `docs/agent-orchestration-map.md` for:

$ARGUMENTS

Follow it exactly. Do not skip steps, and do not collapse two agents into one call.

**Step 0 — sensitive-path check.** Compare the likely blast radius against
`.claude/sensitive-paths.md`. If any part touches that list, STOP the loop for that
part: it is authored at the fable tier directly (`/sensitive` has the checklist).
Continue the loop for the non-sensitive remainder only, and say what you split off.

**Step 1 — scope.** Any pure lookup needed to scope this goes to `code-scout`
(haiku) — never to a higher tier and never inline in this session.

**Step 2 — plan.**
- Multi-release, schema-touching, or long-horizon → `planner` (fable), which writes
  `docs/plans/<slug>.md`.
- Shape already mostly clear, single PR → `design` (opus), plan returned in-session.
- Trivial *and* fast-path eligible (single file or file+test, no new public
  behavior, no new dependency, no sensitive path, existing tests cover the area) →
  skip to step 4 with an inline spec, and say you took the fast path.

**Step 3 — spec.** `spec-writer` (opus) turns the plan into `docs/specs/<slug>.md`
with exact files/functions, the pytest cases to add, and mechanical done-criteria.
If `spec-writer` refuses on a sensitive path, do not work around it.

**Step 4 — implement.** `code-writer` (sonnet) executes the spec and nothing more.
If it stops and reports, do NOT re-issue the same instruction — the stop is a
signal that the spec or the plan is wrong. Go back a step.

**Step 5 — verify.** `verifier` (haiku) independently re-runs the gates the spec
named plus the defaults. Its report, not the writer's, is the evidence. A SKIP is
not a PASS.

**Step 6 — review.** `code-reviewer` (fable). If the diff touches a sensitive path,
adds an endpoint or webhook surface, adds a table or migration, or changes an
LLM-spending path, run `/review-diff` instead so `security-reviewer` runs siloed
alongside it.

**Loop bound — hard.** Steps 4→5→6 may repeat at most **2 remediation rounds**. Do
not attempt a third. On exhaustion, STOP and hand up to the fable tier with: the
spec, both diffs, both verifier reports, and every reviewer finding. Two failed
rounds means the plan is wrong, not the code.

Report at the end: which agents ran, the artifacts they produced (paths), the final
gate output, and any finding you consciously did not act on.
