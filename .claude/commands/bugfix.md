---
description: Bug loop (L2) — repro-anchored diagnose → fix → confirm, bounded at 2 fix attempts
argument-hint: <symptom>
---

Run the **bug loop (L2)** from `docs/agent-orchestration-map.md` for:

$ARGUMENTS

The loop is anchored on a red test, not on prose. A fix that has no failing test in
front of it has not been verified, only asserted.

**Step 1 — diagnose.** `bug-hunter` (fable). It must return:
- the reproduction command and its **real** output,
- the root cause at `file:line` with the mechanism explained,
- a proposed fix in prose,
- **the name of the test that must go red → green.** If no such test exists yet,
  it names the test to be written and what it must assert.

Point it at git archaeology (`git log -S`, `git blame`) if the question is "when did
this start".

**Step 2 — route the fix.**
- Root cause in `.claude/sensitive-paths.md` → STOP. Authored at the fable tier
  directly; see `/sensitive`.
- Otherwise → `spec-writer` (opus) if the fix spans files or changes behavior;
  straight to `code-writer` (sonnet) with the diagnosis as an inline spec if it is a
  contained one-file fix.

**Step 3 — write the failing test first.** Confirm it is RED before the fix lands.
Never edit an existing test to make it pass.

**Step 4 — verify.** `verifier` (haiku) runs the named test plus the default gates.
The named test must be GREEN.

**Step 5 — confirm the mechanism.** Re-invoke `bug-hunter` with the diff and the
verifier output. It must confirm that the mechanism it diagnosed is the one that
changed — not merely that the symptom disappeared. A symptom that goes away for a
different reason is an unfixed bug with a passing test.

**Loop bound — hard.** At most **2 fix attempts**. If the second fails, STOP: the
diagnosis was aimed wrong. Send it back to `bug-hunter` at fable for a fresh
diagnosis with both failed diffs attached.
