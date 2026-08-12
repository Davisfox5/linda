# Ask LINDA — tool gaps and agent-graph gaps

Status: **steps 1–6 of §4 implemented** (Tier 0 repairs, `resolve_entity`, the G2
context seam, five Tier 1 reads, `propose_step_dispatch`, and the G5 outcome
loop — this branch). Remaining: G4 outbound turn and G3 job graph. Tools still
open: `get_scorecard`, `list_manager_alerts`, and the Tier 2 update/send verbs.
Date: 2026-08-12.
Framework: [`agent-infrastructure-knowledge-base.md`](../../agent-infrastructure-knowledge-base.md)
(harness → router → feedback loops), applied to the Ask LINDA chat surface
specifically rather than to the backend's LLM plumbing (which
[`docs/agent-infra-audit.md`](../agent-infra-audit.md) already covers and closed out).

Question this answers: *what tools does Ask LINDA still need in order to do the job
of the agentic system we are building, and what agent graphs are missing so the
agents overlap and loop correctly?*

---

## 0. What Ask LINDA is today (verified)

| Property | Value | Citation |
|---|---|---|
| Model | Sonnet, pinned | `linda_agent.py:1006` (`forced_tier=Tier.SONNET`) |
| Tools | 12 — 7 read, 5 draft | `linda_agent.py:147-392`, `:394-409` |
| Tool loop | flat, single context, **max 5 cycles** | `linda_agent.py:991` |
| Output budget | `max_tokens=2048` per cycle | `linda_agent.py:50` |
| Working memory | most recent **40** messages, boundary-safe | `linda_agent.py:58`, `:805-826` |
| Cross-conversation memory | **none** | — |
| Stream budget | **120 s** hard server-side lifetime | `api/chat.py:98` |
| Writes | staged as `WriteProposal`, user confirms in UI | `linda_agent.py:746-766`, `api/chat.py:254-286` |
| Subagents / delegation | **none at runtime** | — |
| Self-check / critic | **none** | — |
| Chat-specific evals | **none** (`llm_judge` covers analysis insights, not chat) | `services/llm_judge.py` |

So: a competent single-agent harness with a bounded, grounded tool loop and a
human-in-the-loop write gate — and essentially nothing above that. The audit's
Layer A work (catalog, router, failover, history window) is done; what is missing
now is **reach** (tools) and **shape** (graphs).

Hygiene note: `LINDA_MODEL = model_catalog.SONNET` (`linda_agent.py:49`) is dead —
the router pins the tier directly at `:1006`. Delete or use it; a stale second
model seam is exactly what the catalog rule exists to prevent.

---

## Part 1 — Tools

### Tier 0 — three shipped tools that cannot complete ✅ FIXED

> **Done on this branch.** `propose_crm_update` now executes through the CRM
> writeback adapters on confirm (`api/chat.py::_execute_crm_update`) and its schema
> was reshaped from the unexecutable `{target, fields}` to `{interaction_id,
> operation, payload}` matching `CrmAdapter.execute_operation`; `propose_action_item`
> and `propose_email_draft` now require `interaction_id` in-schema; `resolve_entity`
> (T1.1) makes `propose_queue_bump_email` reachable. A fourth bug surfaced while
> fixing these — see the status-vocabulary note at the end of this section.


These are not "missing capability", they are **broken promises**: the model calls
them, the UI shows a proposal card, the user clicks Confirm, and nothing (or an
error) happens. Every one of them teaches the user that Linda's writes are fake.

**T0.1 — `propose_crm_update` confirms to a no-op.**
`api/chat.py:398-402` returns `None` with a comment saying a Celery worker can
pick it up. Nothing does: `WriteProposal` is referenced only by `linda_agent.py`,
`api/chat.py`, and `models.py`. The proposal flips to `confirmed` with a null
`resulting_entity_id` and no CRM write ever occurs. Either wire it to
`services/crm/` (the same adapters `action_plan/dispatch.py` uses) or remove the
tool. Shipping a confirm button that does nothing is worse than not offering it.

**T0.2 — `propose_action_item` and `propose_email_draft` have schemas that
disagree with their executors.**
The tool schemas require only `title` (`linda_agent.py:291`) and
`subject/body/recipients` (`:303`); the executors hard-fail with **422** when
`interaction_id` is absent or unparseable (`api/chat.py:331-335`, `:376-380`).
Nothing in the schema descriptions or `PERSONA` tells the model `interaction_id`
is mandatory, so "Linda, remind me to call Acme Friday" produces a proposal card
that 422s on Confirm. Fix by making `interaction_id` required in the schema **or**
by allowing standalone items in the executor — the second is the better product
answer, since not every follow-up comes from a call.

**T0.3 — `propose_queue_bump_email` is unreachable.**
It requires `prospect_id` — a `Customer` UUID (`linda_agent.py:318-322`) — and
**no tool in the registry ever returns a customer id**. `search_interactions`
returns `interaction_id / score / highlights / summary / channel / created_at`
(`search_service.py:120-135`); `get_interaction_detail` returns interaction fields
and snippets only (`linda_agent.py:503-523`). The model's only path to a valid
`prospect_id` is the user pasting a UUID into chat. This is the single clearest
illustration of the structural gap below.

### The structural gap: Linda has no entity resolution

Every write tool is keyed on an id (`interaction_id`, `prospect_id`,
`campaign_id`), and the read tools return exactly one kind of id
(`interaction_id`) plus, since #200, `campaign_id`. There is **no tool that turns a
name into an id** — no "Acme Corp" → customer, no "Dana" → user, no "the pricing
thread" → contact. The agent therefore cannot chain: it can find a call, but it
cannot act on the *person* the call was with.

**T1.1 — `resolve_entity` (highest-leverage single tool in this document). ✅ SHIPPED**
Name / email / domain fragment → candidate `{kind, id, display_name, confidence}`
rows across `Customer`, `Contact`, and `User`, with a customer's
`pipeline_status` / `do_not_contact` carried inline so the model doesn't propose a
bump the confirm endpoint will 409. Implemented as deterministic SQL in
`services/linda_entity_lookup.py` — it is a lookup over rows that already exist,
not inference; the fuzzy model-driven resolution stays in `entity_resolution.py`
where the pipeline uses it. It unblocks T0.3 and every Tier-2 write below.

**Bug found while fixing Tier 0 — action-item status vocabulary. ✅ FIXED**
`ActionItem.status` is canonically `{open, done, dismissed}`
(`api/action_items.py:168-172`), but the chat executor created items with
`status="pending"` and the `get_action_items` tool advertised
`pending/in_progress/completed`. `list_action_items(status="open")` expands to
exactly `["open"]`, so **every action item Linda ever created was invisible in the
SPA's Open filter**, and asking Linda for "completed" items always returned zero
rows. Chat now writes `open` and the tool normalizes legacy spellings on read
(matching all of them in the `open` bucket, so pre-existing rows resurface).

### Tier 1 — reads the product already promises but cannot deliver

> **Five shipped on this branch** (`services/linda_reads.py`): `get_customer_360`,
> `search_knowledge_base`, `get_profile`, `get_team_metrics`, and
> `list_action_plans`. The last was originally held back with T1.4/T1.7 — it came
> forward because `propose_step_dispatch` is keyed on a `step_id` that no other
> read returns, and shipping a write whose id has no source is precisely T0.3.
> `get_scorecard` (T1.4) and `list_manager_alerts` (T1.7) remain open.
>
> **Correction to §3 below.** This document originally said an API-key caller
> (`AgentContext.user is None`) should be denied on `get_profile`, "deny is the
> safe default". That was wrong: `auth.py:512` documents API keys as programmatic
> tenant-wide credentials and builds their principal with `role="admin"`. Denying
> in chat would refuse the very same key that can `GET /api/v1/profiles/business`
> directly — friction with no security gain. The implementation matches `auth.py`,
> and `build_principal` carries the reasoning.
>
> `get_profile` runs the **same** `_authorize_*` gates as `api/profiles.py`
> (imported, not restated) and `get_team_metrics` delegates to the analytics
> dashboard endpoint, so a chat answer and the SPA cannot quote different numbers.
> Denials use one message for "not allowed" and "doesn't exist" so the tool isn't
> an existence oracle.


`PRODUCT_KNOWLEDGE` (`linda_agent.py:79-119`) tells the model that Scorecards,
Snippets, Live Coaching, Integrations and Webhooks are core surfaces. There is no
tool for any of them. The prompt advertises a product the tool registry does not
implement, which is precisely the failure mode that produced the campaign incident
in [`campaign-monitoring.md`](campaign-monitoring.md) — the model either
hallucinates or disclaims.

| # | Tool | Backed by (already exists) | Why it matters |
|---|---|---|---|
| T1.2 ✅ | `get_customer_360` | `cs_account_health.compute_health_score`, `CustomerConcern` / `CustomerCommitment` (`models.py:3428,3520`), `CustomerNote` / `CustomerWarning`, `Customer.pipeline_status` | "What's going on with Acme?" is the most natural question a user will ask and Linda currently answers it by full-text searching transcripts. |
| T1.3 ✅ | `get_profile` | `ClientProfile` / `AgentProfile` / `ManagerProfile` / `BusinessProfile` (`models.py:2142-2206`), exposed at `api/profiles.py` | **The orchestrator's four profile trees are the product's brain and chat cannot see them.** Opus spends nightly + weekly cycles maintaining them (`orchestrator.py:1-22`) and no runtime reader consumes them for chat. **Constraint:** `api/profiles.py:1-17` enforces role-scoped access (agent → own; manager → reports; admin → all). The tool layer scopes by tenant only today, so this tool must carry that RBAC itself — see §3. |
| T1.4 | `get_scorecard` | `InteractionScore`, `ScorecardTemplate` (`models.py:1358,1370`), `scorecard_service.py` | Directly promised by the system prompt. |
| T1.5 ✅ | `search_knowledge_base` | `kb_document_retrieval.retrieve` (Qdrant RAG) | The tenant's own policies/playbooks. Without it Linda answers "what's our refund policy" from the base model — the highest-risk hallucination surface in the product. |
| T1.6 ✅ | `get_team_metrics` | `tenant_insights_service.aggregate_tenant_period`, `api/analytics.py` | "How did the team do this week?" today costs N searches and blows the 5-cycle cap. Aggregates must be one tool call, not a loop. |
| T1.7 | `list_manager_alerts` | `ManagerAlert` (`models.py:3223`), fed by `anomaly_detector.py` + `campaign_monitor.py` | The proactive detectors already fire; chat can't read what they found, so the user hears about an alert in Slack and Linda knows nothing about it. |
| T1.8 ✅ | `get_action_plan` / `list_action_plans` | `ActionPlan` / `ActionStep` (`models.py:898,965`) | Linda can *create* a plan (`propose_action_plan`) and then cannot read it back. Write-only is not a workflow. |

Deliberately **not** proposed: dump-everything list tools, raw SQL, per-snippet or
per-webhook tools. The knowledge base is explicit (§4, tool design) that a few
targeted tools beat many broad ones, and every extra tool schema is permanent
prompt weight on a cached block.

### Tier 2 — writes: Linda has one hand and it only creates

All five draft tools *create* something. None updates, completes, sends, pauses,
or cancels. An agent that can only ever add rows cannot close a loop.

**T2.1 — `propose_action_item_update`** — complete / reassign / reschedule /
reprioritize. Without it, "mark that done" is impossible and Linda's own created
items accumulate forever.

**T2.2 — `propose_step_dispatch` — the largest single capability unlock in this
document. ✅ SHIPPED** (`services/linda_dispatch.py`)

> Chat is now a **third caller** of `action_plan/dispatch.py`, not a third
> implementation — it routes through the auto-executor's own
> `_dispatch_for_channel`, and a test asserts the two "what can be sent" channel
> sets stay identical.
>
> **On `AUTO_EXECUTION_ENABLED`:** it does *not* gate this, deliberately. That flag
> governs *unattended* dispatch — the executor acting with nobody in the loop, on a
> per-(tenant, action_class) policy that defaults to manual. A user reading a
> proposal card and clicking Confirm **is** the human approval the flag exists to
> require, which is why the manual `/send-email` endpoint is likewise ungated.
>
> **Stricter than the manual endpoints, on purpose.** Those dispatch from any state
> with no artifact checks, because a rep is looking at the rendered artifact when
> they click. A Linda user is approving Linda's *description*, so this path also
> enforces the auto-executor's pre-flight: plan active, step actually actionable
> (no re-sending a `done` step), artifact present with **no `unfilled_slots`** (an
> unfilled artifact still contains literal `{{placeholders}}`), and a dispatchable
> channel. Pre-flight runs **twice** — once before staging, so an un-sendable step
> never becomes a Confirm button, and again at confirm time, because proposals live
> 24h and a step can be sent or regenerated in between.
>
> **`list_action_plans` shipped alongside it** (T1.8), out of the held-back set:
> `propose_step_dispatch` is keyed on a `step_id` and no read tool returned one.
> Shipping the write alone would have recreated T0.3 exactly.
 `action_plan/dispatch.py` is *already* the single code path that sends
an email, writes a CRM note/task, or books a calendar event for an `ActionStep`,
and it is already shared by the manual endpoints and the governed auto-executor
(`action_plan/executor.py`, gated behind `settings.AUTO_EXECUTION_ENABLED` +
per-(tenant, action_class) policy). **The hands exist, they are safety-gated, and
Linda cannot reach them.** Routing a proposal into `dispatch_step_*` gives chat
real-world effect while inheriting the existing governance — no new safety surface
is invented, and the `WriteProposal` confirm is a strictly stronger gate than the
policy check the executor uses.

**T2.3 — `propose_send_email` (real send).** Today `email_draft` confirms into a
staged `ActionItem.email_draft` blob (`api/chat.py:367-396`) — a note, not a
message. Actual sending exists in both the outreach scheduler and
`action_plan/dispatch.py`.

**T2.4 — `propose_campaign_action`** — pause / resume / adjust daily quota /
halt a member on an outreach campaign. The REST surface landed in #199; chat can
read campaigns (#200/#201) but can only ever *watch* them.

### Tier 3 — the meta-tools an agentic system needs

**T3.1 — `remember` (durable memory write).** There is no cross-conversation
memory at all: `_load_history` is scoped to one `conversation_id`
(`linda_agent.py:917-943`) and windowed to 40 messages. Every new conversation
starts from zero, forever. The knowledge base calls tiered memory a core harness
component (§1) and prefers async writes off the response path.

**T3.2 — `schedule_followup`.** "Check the pipeline every Monday and tell me
what slipped." Requires subscription state — the same open question deferred as
Phase 3.3 of [`campaign-monitoring.md`](campaign-monitoring.md) (new table +
RLS guard-test update, or an existing per-user preferences JSONB).

**T3.3 — `notify_human`.** `notification_service` + `manager_alert_fanout`
(Slack, severity-gated) exist; Linda cannot escalate to a person.

---

## Part 2 — Agent graphs

### What graphs exist today

| Graph | Shape | Loop? |
|---|---|---|
| Ingest → analysis pipeline (`tasks.py` + `pipeline_ledger.py`) | linear DAG, exactly-once via ledger claims | no (deterministic workflow — correctly so) |
| Orchestrator profiles (`orchestrator.py`) | realtime Sonnet delta → daily Opus consolidation → weekly Opus calibration | **cross-attempt loop, but open at the far end** — no runtime reader feeds profiles back into chat |
| Action plans (`action_plan/engine.py`) | true DAG: `depends_on`, slot propagation, debounced regen, cascade on skip/delete | state machine, no agent drives it |
| Monitor → alert → fanout (`anomaly_detector`, `campaign_monitor`) | scan → detect → Haiku render → `ManagerAlert` → Slack/in-app | terminal — nothing comes back |
| Eval flywheel (`llm_judge` → `insight_quality_scores` → `regression_watchdog` / `variant_rollout` / few-shot pools) | closed loop | ✅ closed — **for analysis only, not for chat** |
| Ask LINDA (`linda_agent.run_chat_turn`) | flat tool loop, 1 model, ≤5 cycles | bounded and grounded, but no plan step, no critic, no delegation |

The repo already knows how to build good graphs. None of them touch chat.

### G1 — Ask LINDA needs a plan → act → verify shape, not a flat loop

Today the model improvises tool calls one at a time inside a 5-cycle budget
(`linda_agent.py:991`), 2048 output tokens per cycle, inside a 120 s stream
(`api/chat.py:98`). Multi-hop asks ("which at-risk accounts have no follow-up
scheduled?") hit the cap and emit the cycle-limit error at `:1094-1100`.

Add two bounded stages around the existing loop:

1. **Plan** (cheap tier, one call, only when the request is multi-hop) — decompose
   into named steps so the tool budget is spent deliberately rather than
   discovered.
2. **Verify** (only for claims that are checkable) — re-run the deterministic
   query behind a numeric or aggregate claim and compare. This must be
   **grounded**: recompute the number, re-query the source. A second Sonnet call
   asked "is this answer good?" is the coherence trap (knowledge base §3) and can
   lower accuracy. If nothing checkable was asserted, skip the stage entirely.

Cap both; terminate on no-improvement. This is the evaluator-optimizer pattern the
knowledge base names first-class (§4) and the one pattern the repo has never
applied to a customer-facing surface.

### G2 — retrieval subgraph (context isolation) ✅ SHIPPED

> **Done on this branch** as `services/linda_context.py`, wired into
> `run_chat_turn` before history/persistence. Two stages: deterministic
> projection (cap text fields → drop trailing rows → tighten text if the row floor
> still overshoots) always runs; a Haiku sub-call in its own context re-selects
> rows against the user's question only when stage 1 would drop some, and only for
> free-text search tools. Two safety rules made it into code and tests: **numbers
> never pass through a model** (campaign rollups/funnels/quotas are
> deterministic-only), and **the model's output is verified, not trusted** — rows
> whose id wasn't in the input are discarded, and if verification empties the
> selection the deterministic result stands. Any failure falls back. The reduction
> is disclosed to the model in-band so it can't report "3 results" when there were
> 40. Config: `LINDA_TOOL_RESULT_BUDGET_CHARS`, `LINDA_CONDENSE_ENABLED`.


Every tool result is `json.dumps`'d whole into the main context
(`linda_agent.py:1070`). Adding T1.2–T1.8 without changing that will bloat the
40-message window with raw rows and walk the chat straight into context rot — the
knowledge base's dominant single-agent failure mode (§5), whose named countermeasure
is exactly **context isolation via subagents**. The repo applies that discipline at
build time (`.claude/agents/`, `code-scout` on Haiku) and nowhere at runtime.

Shape: a Haiku-tier *retrieve-and-condense* subcall for the wide tools
(`search_interactions`, `search_knowledge_base`, `get_team_metrics`) that returns a
distilled, citation-carrying result to the main Sonnet context instead of raw rows.
Cheap tier, isolated context, and it makes the wide reads affordable rather than
dangerous.

### G3 — long-running work must leave the request path

The 120 s stream lifetime and 5-cycle cap are hard ceilings on ambition. "Review
all 40 at-risk accounts and draft outreach for each" is not a chat turn; it is a
**job**. Nothing in the system can currently express that: `WriteProposal` is the
only async handoff and it is single-shot and user-confirmed.

Shape: chat spawns a Celery-backed Linda task with its own `pipeline_ledger` claim
(the exactly-once machinery already exists), progress events over the existing
notification/SSE layer, and a completion that posts back into the conversation as a
Linda-authored turn (which needs G4's initiation path). Per-item work fans out with
isolated context; the parent only sees summaries. This is the orchestrator-workers
pattern (§4), and it is the difference between a chat assistant and an agentic
system.

### G4 — close the proactive ↔ conversational loop

`campaign_monitor` and `anomaly_detector` detect well and then dead-end into a
Slack message. Two edges are missing, and they point in opposite directions:

- **inbound:** alerts become Linda-readable (T1.7), so a user who saw a Slack
  alert can ask Linda about it and get a grounded answer;
- **outbound:** Linda gets a **system-initiated conversation turn**, so a detection
  becomes "Acme's bounce rate tripled on Tuesday — want me to pause the campaign?"
  with the `WriteProposal` already attached. Today only a human can start a turn
  (`api/chat.py:219-251`).

The outbound edge is what makes the system feel agentic rather than reactive, and
it is the natural home for the subscription state deferred in
[`campaign-monitoring.md`](campaign-monitoring.md) Phase 3.3.

### G5 — outcome feedback: nothing measures whether Linda's actions worked ✅ SHIPPED

> **Done on this branch.** New `linda_action_outcomes` table (migration
> `lo_001_linda_outcomes`, RLS-covered via the standard new-table checklist) plus
> `services/linda_outcomes.py` and a daily `linda_outcome_scan` beat task.
>
> **The loop is grounded by construction.** Every verdict traces to a real row
> changing — an action item closing, an outreach member replying, a step
> completing. No model judges anything, and a test asserts the module never
> imports the router or catalog: an LLM scoring whether its own suggestion was
> good is the coherence trap this loop exists to avoid.
>
> **Cancels are now evidence.** They were discarded entirely; a cancel records
> `decision=cancelled, outcome=rejected` immediately.
>
> **`no_signal` is kept distinct from failure**, in both the vocabulary and the
> rate arithmetic: `success_rate` divides only by *resolved* outcomes, so pending
> and no-signal rows don't drag a kind's score down simply for being recent.
> Expiries are `no_signal` too — a user who never came back said nothing about
> whether the proposal was good.
>
> `acceptance_summary()` is the first consumer: per-kind confirm rate and success
> rate. A kind users routinely cancel is a tool-description or threshold problem,
> which is exactly the signal that was missing.


`WriteProposal` records `confirmed_at` and `resulting_entity_id`. Nothing ever
looks at what happened next — did the bump email get a reply, did the CRM update
stick, did the action item get closed, was the proposal cancelled (a strong
negative signal we currently discard).

The analysis side already has this flywheel: `llm_judge` → `insight_quality_scores`
→ `regression_watchdog` / `variant_rollout` / `refresh_few_shot_pools`. Chat has
none of it. Without outcome scoring there is no principled way to tune Linda's tool
descriptions, its tier, or its proposal thresholds — every change is taste.

Shape: proposal → execution → **observed outcome** (deterministic joins:
`CampaignEvent` reply, `ActionItem.status`, `CrmSyncLog`) → a chat-quality table
feeding the existing watchdog. Deterministic where possible, judge only where
genuinely subjective (§6). Score the **trajectory** — which tools were called, in
what order, with what arguments — not just the final text, so a bad score points at
the step that caused it.

### G6 — memory graph

Working memory is windowed correctly. Episodic (across conversations in a session)
and semantic (durable tenant/customer knowledge) memory do not exist for chat, and
the stores that *would* back them — profiles, `customer_memory`, KB — are
unreadable from chat (T1.2, T1.3, T1.5) and unwritable (T3.1). Read edges come free
with Tier 1; the write edge should be async, off the response path.

---

## 3. Constraints any of this work must hold

- **Every write stays behind `WriteProposal` + explicit user confirm.** Widening
  reach must not widen autonomy in the same change.
- **RBAC belongs in the tool layer.** Chat tools scope by tenant only
  (`linda_agent.py:450`, `:489`). That matches the interactions REST surface, but
  **not** `api/profiles.py:1-17`, which is role-scoped. ✅ Handled for T1.3 by
  *importing* those gates rather than re-implementing them — a second copy of a
  security rule eventually disagrees with the first. **Superseded:** the original
  text here said API-key callers (`AgentContext.user is None`) should be denied;
  see the correction under Tier 1 — they are tenant-admin, per `auth.py:512`.
- **Sync/threaded tools must re-arm the tenant GUC.** `_fetch_sent_gmail_sync`
  (`linda_agent.py:594-716`) is the correct precedent: `tenant_context(...)` around
  every query, or RLS fails closed.
- **Layer A rules hold:** model ids only via `model_catalog`, calls only via
  `ModelRouter` / `acreate_with_failover`, Haiku/Sonnet/Opus only, no Fable.
  Python 3.9 typing (`Optional[X]`, not `X | None`).
- **New tables need RLS classification + the guard test**
  (`tests/test_rls_scoping_guard.py`) — relevant to T3.2, G4 subscriptions, and
  G5's scoring table. Those are sensitive-path changes per `CLAUDE.md`.
- **Tool schemas are cached prompt weight.** Adding 15 tools at once degrades
  selection accuracy. Land them in the tiers below and measure.

---

## 4. Suggested sequence

1. ~~**Tier 0 repairs** (T0.1–T0.3)~~ — ✅ done on this branch.
2. ~~**T1.1 `resolve_entity`**~~ — ✅ done on this branch.
3. ~~**G2 retrieval subgraph**~~ — ✅ done on this branch; the wide reads can now
   land without each one bloating the window.
4. ~~**Tier 1 reads**, prioritised `get_customer_360` → `search_knowledge_base` →
   `get_profile` → `get_team_metrics`~~ — ✅ those four done on this branch; the
   remaining three (`get_scorecard`, `list_manager_alerts`, `get_action_plan`)
   are held until tool-selection accuracy has been observed at 16 tools.
5. ~~**T2.2 `propose_step_dispatch`**~~ — ✅ done on this branch, with
   `list_action_plans` alongside it (a write tool needs a read that returns its id).
6. ~~**G5 outcome loop**~~ — ✅ done on this branch; the flywheel now has data.
7. **G4 outbound turn + G3 job graph** — the genuinely new infrastructure; do it
   last, on top of a tool surface that has been measured.

G1's verify stage can land any time after step 4; it is cheap and independent.
