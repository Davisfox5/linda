# docs/plans/ — strategy and rollout plans

Owned by the `planner` agent (fable). `planner` writes only under `docs/`; specs
belong to `spec-writer` in [`../specs/`](../specs/).

Use `planner` for genuinely hard, long-horizon work: multi-release rollout
sequencing, refactors spanning the FastAPI backend / Celery pipeline / Next.js SPA,
anything with a schema change. Single-PR work whose shape is already clear goes to
`design` (opus) and returns in-session without a file.

See [`../agent-orchestration-map.md`](../agent-orchestration-map.md) §2 for the
handoff contract.

## Required contents

- **Approach**, grounded in the real code with `file:line` evidence.
- **Ordered, reversible steps** with explicit checkpoints, precise enough that
  `spec-writer` can turn each step into a spec near-one-shot.
- **Where each step's verification comes from.**
- **Risks and open questions** — flagged, never assumed away.
- **Which steps are sensitive-path** (see
  [`../../.claude/sensitive-paths.md`](../../.claude/sensitive-paths.md)) and must be
  authored at the fable tier — `spec-writer` and `code-writer` will refuse them.

## Deployment realities every plan must respect

- Push to `main` auto-deploys staging on Fly.io.
- Alembic migrations run via `release_command` **before** new code boots, while old
  code is still serving. Schema plans sequence expand → backfill → contract across
  releases.
- Tenant isolation is fail-closed RLS. A plan that adds tables must include
  `rls.py` registration and the `tests/test_rls_scoping_guard.py` update.
- Runtime LLM changes stay behind `backend/app/services/model_catalog.py` and
  `ModelRouter` / `acreate_with_failover` — never a hardcoded model id.
