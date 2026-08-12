# Sensitive paths — canonical list (LINDA)

**This file is the single source of truth.** The same list is repeated inside
several agent prompts (`spec-writer`, `code-writer`, `code-reviewer`, `bug-hunter`,
`planner`, `implement`) because an agent must be able to honor the rule without
reading another file. **If a prompt and this file disagree, this file wins**, and the
drift is itself a finding — `/preflight` checks for it.

## The list

| Path | Why it is sensitive |
|---|---|
| `backend/app/rls.py` | Fail-closed RLS policy DDL + table classification. A missed registration is a cross-tenant leak. |
| `backend/app/tenant_ctx.py` | Tenant GUC binding. Break it and every query silently returns zero rows — or worse, all rows. |
| `backend/app/auth.py` | API-key scopes, JWT sessions, role hierarchy (agent < manager < admin). |
| `backend/app/api/stripe_webhook.py` | Unauthenticated surface; HMAC-SHA256 is the only gate. |
| `backend/app/services/stripe_billing.py` | Replay window, entitlement grants, dual-secret rotation. |
| `backend/app/services/token_crypto.py` | Fernet encryption; the dev-key fallback must stay unreachable in production. |
| `backend/alembic/versions/` | Migrations run via Fly `release_command` **before** new code boots, while old code still serves. |
| `backend/app/models.py` (schema changes only) | Schema drift against migrations; new tables need `tenant_id` + `rls.py` registration. |
| `fly.toml`, `fly.production.toml` | Deploy topology, release command, secrets wiring. |
| `.github/workflows/ci-cd.yml` | The test gate itself, including the Postgres service the RLS isolation tests depend on. |

## The rule

Specs and edits touching anything above are **authored at the fable tier directly**.
`spec-writer` (opus) and `code-writer` (sonnet) must REFUSE and report back — this is
a fixed external trigger, not a judgment call. Do not edit around a sensitive path,
and do not write a partial spec that quietly excludes it.

`implement` (sonnet) is bound by the same refusal. It was previously not, which made
routing a change to `implement` instead of `code-writer` a silent bypass.

## Caveat (from CLAUDE.md, restated)

This rule trades a small fable increase for the large scout/writer reduction. If
sensitive-path work ever comes to dominate the workload, revisit it — at that point
the fable increase may outweigh the savings.

## Known hole

No agent in this repo can both run on fable **and** write source: the fable agents
are read-only or docs-only, and every agent with `Edit`/`Write` runs on sonnet or
opus. "Authored at the fable tier directly" therefore means **the main session,
running on a fable-class model**. See
[`../docs/agent-orchestration-map.md` §4](../docs/agent-orchestration-map.md) for the
two ways to close this and which one is currently in force.
