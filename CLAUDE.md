# CLAUDE.md — working conventions for this repo

## How to report back (standing preference)

**Any answer longer than a few paragraphs gets written in plain language.** Not a
jargon summary bolted onto a technical wall — the whole thing, plain, first time,
without being asked.

- Short sentences. Say what broke and what it means for the product.
- Skip file paths, symbol names, and API terminology unless they're the point.
- Lead with the outcome, then what changed, then what's left.
- Corrections and dropped work get stated plainly — "I said X, I was wrong, here's
  why" — not buried.
- Anything that changes live customer behavior: ask before flipping it on.

Technical depth on request, not by default.

Architecture lives in [ARCHITECTURE.md](ARCHITECTURE.md). For agent/LLM-infra
decisions, follow [agent-infrastructure-knowledge-base.md](agent-infrastructure-knowledge-base.md)
and the audit in [docs/agent-infra-audit.md](docs/agent-infra-audit.md).

How the dev subagents coordinate — loops, handoff artifacts, escalation ladder,
write territories — is in
[docs/agent-orchestration-map.md](docs/agent-orchestration-map.md). This file says
*which* agent; that file says *what happens next*.

## Runtime LLM rule (Layer A — the shipped app)

- **Every runtime model id resolves through `backend/app/services/model_catalog.py`.**
  Never hardcode a `claude-*` string anywhere else. `tests/test_model_catalog.py`
  has a guard test that fails the build if you do. Bumping a version or swapping a
  deprecated/suspended model is a one-line change in the catalog / an env override.
- Runtime uses only **Haiku / Sonnet / Opus**, each touchpoint on the cheapest tier
  that meets its quality bar. **Fable (Mythos-class) is never called from app code.**
- Live model calls go through `acreate_with_failover` (in `llm_client.py`) or
  `ModelRouter` so a provider blip retries/fails over instead of failing the request.
- System Python is **3.9**: use `Optional[X]` / `Dict` / `List`, not `X | None`.
- Before pushing code that adds routers / decorators / import-time work, run
  `python3 -c "from backend.app.main import app"`.

## Model routing (dev — Layer B, how Claude Code spends tokens here)

**Scope: this section governs Claude Code working ON this repo only.** It never
applies to the application's own LLM calls — runtime model selection (Layer A) is
governed exclusively by `backend/app/services/model_catalog.py` / `ModelRouter` per
the Layer A rules above, and nothing under `.claude/` is read at runtime.

Routing is **top-down**: the highest tier does the judgment work and delegates DOWN
to cheaper tiers for mechanical work. No agent ever self-assesses its own capability
and escalates upward — escalation triggers are external only (a failing test, the
fixed sensitive-path rule below), decided by the caller.

| Agent | Model | Invoke when |
|---|---|---|
| `codebase-analyst` | fable | Architecture questions, tracing data/control flow, "why does this behave this way". |
| `code-reviewer` | fable | Reviewing a diff/PR (includes migration-safety + RLS + sensitive-path checklist). |
| `planner` | fable | Hardest refactor strategies, roadmaps, rollout sequencing (writes to `docs/` only). |
| `bug-hunter` | fable | Reproducing/localizing bugs; proposes fixes, never writes them. |
| `security-reviewer` | fable | Auditing auth/RLS/Stripe/crypto/dependency risk surface. |
| `spec-writer` | opus | Turning a fable-tier plan into a precise spec in `docs/specs/`. |
| `design` | opus | Single-PR planning where the solution shape is mostly clear; plan returned in-session, no file. |
| `code-writer` | sonnet | Implementing against a written spec; runs tests, shows output; stops if blocked. |
| `implement` | sonnet | The **no-spec fast path**: routine well-specified edits with no formal spec. Bound by the same sensitive-path refusal as `code-writer`. |
| `researcher` | sonnet | External library/API docs, pinned-version-first. |
| `code-scout` | haiku | Pure lookups: "where is X / list call sites of Y". **Mandatory for pure search.** |
| `explore` | haiku | Read-only **sweeps**: inventory a directory, map a subsystem. Single-symbol lookups are `code-scout`'s. |
| `verifier` | haiku | Siloed re-run of the repo's gates after any change. Reports PASS/FAIL/SKIP with real output; never fixes. |

Overlapping pairs are split by **input or output, not by capability** —
`code-writer` needs a spec / `implement` does not; `planner` (fable) writes
`docs/plans/` for multi-release work / `design` (opus) returns a single-PR plan
in-session; `code-scout` takes one symbol / `explore` sweeps. See
[docs/agent-orchestration-map.md](docs/agent-orchestration-map.md) §6.

## Loops (dev — Layer B)

Agents are not one-shot delegations. Each loop is **bounded** (hard iteration cap)
and **grounded** (terminates on an external signal — a test result, a finding with
`file:line` — never on an agent's self-assessment), the same discipline
[docs/agent-infra-audit.md](docs/agent-infra-audit.md) §(c) applies to Layer A.

| Loop | Command | Bound | Grounding signal |
|---|---|---|---|
| L1 delivery: spec → write → verify → review → fix | `/feature` | 2 remediation rounds | verifier gate output + reviewer findings |
| L2 bug: diagnose → fix → confirm mechanism | `/bugfix` | 2 fix attempts | the named test going red → green |
| L3 overlapping review (code + security, **siloed**) | `/review-diff` | 1 round each | `file:line` + concrete failure scenario |
| L4 preflight gates before push | `/preflight` | 2 repair attempts | exit codes |
| L5 research → verify against the pin | — | 1 verification pass | the installed code |
| L6 sensitive-path ratchet (no loop) | `/sensitive` | — | — |

Do **not** add: reviewer↔writer ping-pong past 2 rounds (past that the plan is
wrong, not the code), ungrounded self-critique passes, agents that spawn agents
(fan-out stays visible in the main session), or an agentic loop where a deterministic
script would do.

Fixed rules (external triggers, not judgment calls):

- **Sensitive-path rule:** specs and edits touching `backend/app/rls.py`,
  `backend/app/tenant_ctx.py`, `backend/app/auth.py`,
  `backend/app/api/stripe_webhook.py`, `backend/app/services/stripe_billing.py`,
  `backend/app/services/token_crypto.py`, `backend/alembic/versions/`, schema
  changes in `backend/app/models.py`, `fly.toml`, `fly.production.toml`, or
  `.github/workflows/ci-cd.yml` are authored at the **fable tier directly** —
  `spec-writer`, `code-writer` and `implement` refuse those paths and report back.
  Canonical list: [.claude/sensitive-paths.md](.claude/sensitive-paths.md) — **if a
  prompt and that file disagree, that file wins** and the drift is a finding.
  *Caveat: this trades a small fable increase for the large scout/writer reduction;
  if sensitive-path work ever dominates the workload, revisit this rule.*
  *Known hole:* no agent both runs on fable and can write source (the fable agents
  are read-only or docs-only), so "fable tier directly" means **the main session,
  running a fable-class model** — see the orchestration map §4 for the two ways to
  close this.
- **Scout-first rule:** any pure lookup goes to `code-scout` (haiku), never to
  `codebase-analyst` or the main session. This is the main top-tier cost reduction.
- **Researcher-output-is-unverified rule:** `researcher` output is always treated as
  unverified claims; fable-tier consumers and `code-writer` re-verify against the
  pinned versions in `requirements.txt` / `apps/app/package.json` before acting. The
  verification is a *step* (L5), not a disposition: each load-bearing claim ends up
  explicitly **verified** or **dropped**. An unverifiable load-bearing claim means
  change the approach, not loop again.
- **Fast-path rule:** a change may skip `spec-writer` and go straight to
  `implement`/`code-writer` only if it is a single file (or file + its test), adds no
  new public behavior, adds no dependency, touches no sensitive path, and is already
  covered by existing tests. Anything else gets a spec — but routing a one-line fix
  through an opus spec costs more than the fix.
- **Escalation lands somewhere.** When an agent stops and reports, the caller does
  not re-issue the same instruction: a stop means the *input* was wrong. Go back one
  step (writer → spec → plan). See the escalation ladder in the orchestration map §4.

Enforcement layers — what binds vs. what steers:

- **Mechanically enforced** (applied by the harness on every invocation): each
  agent's `model:` and `tools:` frontmatter in `.claude/agents/*.md`.
- **Advisory** (prompt/CLAUDE.md-level, ~70% adherence): whether to delegate at all,
  the scout-first habit, and path restrictions inside prompts (`planner` → `docs/`
  only, `spec-writer` → `docs/specs/` only, the sensitive-path refusals) —
  frontmatter cannot scope write paths. CLAUDE.md steers; frontmatter binds.
- **Not yet wired, highest-leverage next step:** a `PreToolUse` hook in
  `.claude/settings.json` inspecting `tool_input.file_path` would convert every
  advisory path rule above into mechanical enforcement. It is deliberately not landed
  blind — a hook applies to the main session too, so a bad matcher blocks legitimate
  work in every future session. Author and test it interactively. (`.gitignore` now
  versions `.claude/commands/` and `.claude/sensitive-paths.md` alongside `agents/`;
  add `!.claude/settings.json` when the hook exists.)

Other notes:

- Prefer delegating read-heavy exploration to `code-scout`/`explore` rather than
  reading many files in the main session (context-rot control).
- **Fable is a deliberate, top-down choice** — pinned on the five judgment-heavy
  agents above and invoked per this table, never as a default for mechanical work.
  It is capacity-constrained (~2× an Opus call); the tiering above exists to produce
  a net reduction in fable usage versus routing all agent work there.
- Layer B config (`.claude/`) must never change runtime application behavior.
