---
description: Sensitive-path ratchet (L6) — checklist for work that spec-writer and code-writer must refuse
argument-hint: <path or change being made>
---

Sensitive-path work: $ARGUMENTS

Check it against `.claude/sensitive-paths.md` — that file is canonical and wins over
any list quoted in an agent prompt.

**The ratchet.** This work is authored at the **fable tier directly**. It does not
loop down and back up. `spec-writer` (opus) and `code-writer` (sonnet) will refuse
it, and `implement` (sonnet) must too — do not route around the refusal by picking a
different writer.

Be honest about what "fable tier directly" means here: no agent in this repo both
runs on fable and can write source (the fable agents are read-only or docs-only). So
this means **the main session, running a fable-class model**. If this session is not,
say so before editing rather than after. See §4 of
`docs/agent-orchestration-map.md`.

**Read-only prep is still delegated** — it is cheap and keeps the main context clean:
- `code-scout` (haiku) for the call sites and current locations.
- `security-reviewer` (fable) for a pre-change read of the surface being touched.

**Per-area checklist:**

- **RLS / tenant isolation** (`rls.py`, `tenant_ctx.py`, new tables in `models.py`):
  every tenant-owned table registered; fail-closed behavior preserved; nothing
  queries through the owner-role `DATABASE_URL` where the RLS-enforced
  `APP_DATABASE_URL` is required; `tests/test_rls_scoping_guard.py` updated.
- **Auth** (`auth.py`): new write endpoints register a scope or they 403; role
  hierarchy (agent < manager < admin) respected; no scope-check bypass.
- **Stripe** (`api/stripe_webhook.py`, `services/stripe_billing.py`): HMAC
  verification intact — it is the only gate on an unauthenticated surface; replay
  window preserved; dual-secret rotation (`STRIPE_WEBHOOK_SECRET(_NEXT)`) still
  works; no entitlement granted on an unverified event.
- **Crypto** (`services/token_crypto.py`): key derivation unchanged; the ephemeral
  dev-key fallback stays unreachable in production; no change that could render
  stored ciphertext undecryptable.
- **Migrations** (`alembic/versions/`): migrations run via Fly `release_command`
  BEFORE new code boots while old code still serves — additive first, nullable or
  server-defaulted columns, backfills as separate steps, no drop/rename of anything
  live code still reads (expand → migrate → contract across releases), revision id
  ≤ 32 chars, single linear head, downgrade stated or explicitly waived.
- **Deploy** (`fly.toml`, `fly.production.toml`, `.github/workflows/ci-cd.yml`):
  do not weaken the release command or remove the Postgres service the RLS isolation
  tests depend on — removing it silently disables the tenant-isolation gate.

**After the edit — mandatory, both:** `/preflight`, then `/review-diff` (the
overlapping siloed review). Sensitive-path work never ships on a single reviewer.
