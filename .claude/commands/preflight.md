---
description: Preflight gate loop (L4) — deterministic gates and drift checks before pushing
allowed-tools: Bash, Read, Grep, Glob
---

Run the **preflight loop (L4)** from `docs/agent-orchestration-map.md` before
pushing.

**Deterministic first.** Run these as plain commands. Do not delegate to an agent
unless one fails — most preflight failures are mechanical and a model adds nothing.

1. `git status --porcelain` and `git diff --stat` — know what is actually changing.
2. `pytest -q --ignore=tests/sandbox_demo_ui_test.py`
3. `python3 -c "from backend.app.main import app"` — required after any change to
   routers, decorators, or import-time code.
4. `cd apps/app && npm run lint` — only if files under `apps/app/` changed.

**Drift checks** (cheap, deterministic, catch classes of bug that tests do not):

5. **Stray model ids.** `grep -rn "claude-[a-z0-9-]*" backend/app --include=*.py |
   grep -v model_catalog.py` — must be empty. `tests/test_model_catalog.py` guards
   this; catching it here is faster than catching it in CI.
6. **Alembic head linearity.** Confirm a single head, and that any new revision id is
   ≤ 32 characters (`alembic_version` is `VARCHAR(32)`; there is a guard test).
7. **New tables are RLS-registered.** If `backend/app/models.py` gained a table, it
   needs `tenant_id`, registration in `backend/app/rls.py`, and coverage in
   `tests/test_rls_scoping_guard.py`.
8. **Sensitive-path list drift.** The lists inside `.claude/agents/*.md` and
   `CLAUDE.md` must match `.claude/sensitive-paths.md`. That file wins; a mismatch is
   a finding to fix now, not later.
9. **Dependency advisories**, when `requirements.txt` or `apps/app/package.json`
   changed: `pip-audit` / `npm audit`. (`security-reviewer` is read-only and cannot
   run these — this is where they live.)

**Reporting rules.** Report SKIPs as skips with the reason — the RLS isolation tests
skip without `TEST_POSTGRES_URL`, and a skipped tenant-isolation test is a hole, not
a green light. Never claim a gate passed that you did not run.

**Bound.** At most **2 repair attempts** on a failing gate, then STOP and report.
Repeated mechanical failures usually mean the change is wrong, not the gate.

Push only when every gate is PASS or an explicitly-stated, accepted SKIP.
