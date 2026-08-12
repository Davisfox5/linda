# docs/specs/ — implementation specs

Owned by the `spec-writer` agent (opus). **Only `spec-writer` writes here.**
`planner` writes to `docs/plans/`; nobody else writes to either.

A spec is the handoff artifact between judgment and execution: it is what makes
`code-writer` (sonnet) able to work mechanically, with no design decisions left.
Ambiguity in a spec is a bug in the spec, not a judgment call for the implementer.

See [`../agent-orchestration-map.md`](../agent-orchestration-map.md) §2 for the full
handoff contract.

## Naming

`docs/specs/<slug>.md`, where `<slug>` matches the plan it came from
(`docs/plans/<slug>.md`) when there is one.

## Required contents

- **Goal and scope**, explicitly in/out.
- **Exact files and functions** to change, with current `file:line` references the
  spec author actually verified.
- **Test expectations** — the pytest cases to add (path under `tests/`, what each
  asserts) and the command to run them: `pytest -q tests/<file>`; full suite is
  `pytest -q --ignore=tests/sandbox_demo_ui_test.py`.
- **Repo constraints restated where relevant** — Python 3.9 typing (`Optional[X]`,
  `Dict`, `List`); model ids only via `backend/app/services/model_catalog.py`; live
  LLM calls through `acreate_with_failover` / `ModelRouter`; the import smoke check
  `python3 -c "from backend.app.main import app"` when routers or import-time code
  change.
- **An explicit do-not-touch list.**
- **Done-criteria `code-writer` can verify mechanically** (tests green, smoke check
  passes) and `verifier` can re-run independently.
- **Open questions at the top** for anything ambiguous — never a guess.

## What must NOT be specced here

Anything touching a path in [`../../.claude/sensitive-paths.md`](../../.claude/sensitive-paths.md).
`spec-writer` refuses those and reports back; they are authored at the fable tier
directly. A spec that quietly excludes the sensitive part of a change is worse than
no spec.
