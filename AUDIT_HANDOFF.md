# AUDIT HANDOFF — linda

Fleet audit 2026-07-30 (test-9 control repo). **Severity: MEDIUM (deploys blocked).**

## Findings

CI/CD on `main` went red on 2026-07-23 (run 30006687499) after four green
runs. Tests pass; the failure is the `deploy_staging` job:

```
asyncpg.exceptions.InternalServerError: Your account or project has exceeded
the compute time quota. Upgrade your plan to increase limits.
Error release_command failed running on machine 86ed05ae224218 with exit code 1
```

The Fly.io release command (migrations) can't reach the Neon Postgres
database because the **Neon account/project is out of compute quota**. This
is a billing/plan ceiling, not a code defect — every staging deploy will fail
until the quota resets or the plan changes.

## Why this needs you

Three options, all account-level decisions:

1. **Upgrade the Neon plan** (or wait for the monthly quota reset) — no code
   change needed; re-run the failed workflow afterward.
2. **Reduce compute burn** — Neon autosuspend settings, or point staging at a
   cheaper branch/instance.
3. **Retire staging** if it isn't earning its keep.

## Prompt for Claude Code (run inside this repo, only if you choose option 2/3)

> Staging deploys fail because the Neon database exceeds its compute quota.
> Inspect `.github/workflows/ci-cd.yml` and the Fly staging config
> (`fly.staging.toml` / backend release_command) and implement [chosen
> option]: either gate the deploy_staging job behind a manual
> workflow_dispatch input so failed deploys stop blocking the pipeline, or
> remove the staging deploy job entirely. Verify the workflow YAML parses and
> that the CI (test) jobs are unaffected.
