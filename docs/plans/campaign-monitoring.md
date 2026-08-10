# Plan: Campaign visibility in Ask LINDA + proactive campaign monitoring

Status: Phases 0–2 implemented (2026-08-10, this branch); Phase 3 remains future work
Owner: planner (fable) → spec-writer (opus) → code-writer (sonnet), with
fable-tier direct authoring for the one sensitive-path step (see Phase 2, step 2.1).

## Motivating incident

A user asked Ask LINDA "What have the responses to our most recent email campaign
been?" and got (a) a tool error and (b) a disclaimer that LINDA can't see
campaigns, followed by a plain enumeration of sent mail. Two distinct causes:

1. **Bug (Phase 0, already fixed in working tree):** `_exec_search_interactions`
   lost the `db=ctx.db` argument in commit 90ecf8d, so every call raised
   `TypeError`, swallowed into `{"error": ...}` by the catch-all at
   `backend/app/services/linda_agent.py:357-359`. The fix (restoring `db=ctx.db`,
   now present at `backend/app/services/linda_agent.py:349`) is separate from this
   plan; this doc covers it only as context.
2. **Genuine capability gap:** the chat agent has **no campaign read tool** — its
   read tools are exactly `search_interactions`, `get_action_items`,
   `get_interaction_detail`, `search_sent_email`
   (`backend/app/services/linda_agent.py:311-316`), and the system prompt's
   `PRODUCT_KNOWLEDGE` block (`linda_agent.py:77-103`) never mentions campaigns or
   outreach. The disclaimer was prompt-correct behavior. Rich campaign analytics
   exist but only behind REST routers and Celery tasks.

User expectation going forward: LINDA should *answer* campaign questions in chat
**and** *proactively monitor* campaigns — periodic updates, health alerts, and
suggestions for how to proceed or enhance them.

## Ground truth this plan builds on (verified citations)

- **Two campaign kinds share one table**, discriminated by `Campaign.kind`
  (`backend/app/models.py:2412-2448`): `external` (ESP campaigns pushed in via
  `/campaigns`; passive analysis) and `outreach` (LINDA-originated cold outreach
  with `outreach_members` sequence state; reuses `campaign_recipients` /
  `campaign_events` so reply attribution works for both kinds).
- **External-kind rollup:** `_compute_rollup` (`backend/app/api/campaigns.py:282-347`)
  — sent, opens, clicks, unique human clicks (bot-filtered), replies, bounces,
  unsubscribes, conversions, reply_sentiment_avg over attributed inbound
  interactions. Returns `CampaignRollup` (`api/campaigns.py:106`). Private to the
  router.
- **Outreach-kind funnel/quota:** `_member_states` (`backend/app/api/outreach.py:434-442`),
  `_quota_state` (`outreach.py:445-479`, uses `parse_config` +
  `local_day_bounds_utc` + `OUTREACH_DEFAULT_DAILY_LIMIT` /
  `OUTREACH_TENANT_DAILY_SEND_CAP` settings), members list (`outreach.py:1219`
  area), prospect timeline (`outreach.py:898` area). All private to the router.
- **Reply attribution** stamps `Interaction.campaign_id` and a
  `CampaignEvent(event_type="reply")` (`backend/app/services/email_ingest/ingest.py:452-493`,
  `backend/app/services/outreach/replies.py`).
- **Chat surface:** tools defined in `TOOLS` (`linda_agent.py:131-309`), read-set
  `READ_TOOLS` (`:311`), dispatch in `dispatch_tool` (`:615-629`), agent loop
  capped at `max_loops = 5` (`:831`). Chat runs Sonnet via ModelRouter
  (`LINDA_MODEL = model_catalog.SONNET`, `linda_agent.py:47`). Proposal confirm
  flow lives in `backend/app/api/chat.py:319-612`.
- **Chat gating:** `require_feature("ask_linda")` (`backend/app/api/chat.py:224`,
  flag granted per tier in `backend/app/plans.py:87,112,140,164`) plus
  white-label 404 (`chat.py:35-37` — feature is invisible, not forbidden, for
  `Tenant.is_white_label`). The campaigns/outreach REST routers themselves carry
  **no** feature flag (`backend/app/main.py:215-216`; no `require_feature` in
  either router).
- **Proactive infra precedents:**
  - Beat schedule: `backend/app/tasks.py:210-463` (~43 entries).
  - Per-tenant rollup pattern: `tenant-insights-weekly` (`tasks.py:212-215`) →
    `tenant_insights_service.rollup_all_tenants_weekly`
    (`backend/app/services/tenant_insights_service.py:314-343`) — iterate global
    `Tenant` table, enter `tenant_context` per tenant, **commit per tenant** so
    one tenant's failure can't poison the rest.
  - Scan-then-notify pattern: `manager-anomaly-scan` every 15 min
    (`tasks.py:425-428`, task at `tasks.py:5779-5823`) — deterministic SQL
    detectors (`backend/app/services/anomaly_detector.py`), fingerprint dedup via
    partial unique index `ux_manager_alerts_active_fingerprint`
    (`alembic/versions/aa01b2c3d4e5_manager_view_overhaul.py:117-118`; dedup
    insert at `anomaly_detector.py:1138-1150`), fanout to in-app Notification +
    Slack via `backend/app/services/manager_alert_fanout.py` (severity-gated
    Slack at `:159-175`; in-app uses the **already-valid**
    `NotificationKind.MANAGER_ALERT`, `manager_alert_fanout.py:115`).
  - Haiku-through-router precedent:
    `backend/app/services/manager_recommendation_builder.py:52` (`HAIKU_MODEL =
    model_catalog.HAIKU`) and `:397` (`forced_tier=Tier.HAIKU`).
  - A/B winner selection: `backend/app/services/campaign_winner_service.py`
    (`decide_active_campaigns` at `:48`, Wilson lower bound, ≥30 sends,
    idempotent via existing-Experiment check), beat entry `tasks.py:392-395`.
- **Notifications CHECK is closed:** `NotificationKind` vocabulary
  (`backend/app/services/notifications.py:65-94`) mirrors a DB CHECK
  (`ck_notifications_kind`); it has **no campaign kinds**. Extending it requires
  an Alembic migration — a **sensitive path** (fable-authored).
- **manager_alerts.kind is ALSO CHECK-constrained:** `ck_manager_alerts_kind`
  created with only `("topic_spike", "sentiment_drop", "churn_surge",
  "methodology_drop")` (`alembic/versions/aa01b2c3d4e5_manager_view_overhaul.py:43,100-102`).
  See "Discovered drift" below — this matters for the Phase 2 delivery choice.
- **Campaign webhooks fire to external tenant webhooks only** — nothing internal
  listens: `outreach.email.sent` (`services/outreach/scheduler.py:726`),
  `campaign.completed` (`scheduler.py:784`), `outreach.email.replied/opted_out`
  (`services/outreach/replies.py:187`), `outreach.email.bounced` (`replies.py:285`),
  `prospect.status_changed` (`scheduler.py:139`).
- **RLS:** fail-closed; scoped tables are derived automatically from
  `tenant_id` columns (`backend/app/rls.py:65-76`), guarded by
  `tests/test_rls_scoping_guard.py`. All-tenant beat tasks must iterate the
  global tenants table and enter `tenant_context` per tenant
  (`tasks.py:5804-5820` shows the live pattern).

### Discovered drift (flag for fable verification, independent of this plan)

`anomaly_detector.py` inserts CS/Support alert kinds — `renewal_risk_spike`
(`:633`), `health_score_drop` (`:723`), `csat_drop_support` (`:812`),
`escalation_surge` (`:891`), `ttr_drift` (`:971`) — but no migration in
`backend/alembic/versions/` visibly extends `ck_manager_alerts_kind` beyond the
original four sales kinds (`sen_001` only widened the column to String(48),
`sen_001_reconcile_recommendation_drift.py:99-104`; `dom_002` only added
`domain`). Either those inserts are currently failing the CHECK in staging/prod,
or the constraint was altered out-of-band. **The Phase 2 migration must first
verify the live constraint state and reconcile the full vocabulary** (the same
"reconcile drift" move `sen_001` made for recommendation categories). This is an
open question for the fable author of that migration, not an assumption.

---

## Phase 0 — bug fix (done; context only)

- Fix: restore `db=ctx.db` in `_exec_search_interactions`
  (`linda_agent.py:345-360`). Already in the working tree.
- Regression test to land with it: `tests/test_linda_agent_tools.py` —
  1. call `dispatch_tool(ctx, "search_interactions", {"query": "x"})` with a
     stubbed `SearchService.search` and assert the result has `results`, not
     `error`;
  2. a cheap signature guard: for every name in `READ_TOOLS`, assert
     `dispatch_tool` reaches the executor without `TypeError` when given the
     tool's minimal required args (this is the class of bug that shipped —
     an argument-drop invisible to the LLM because of the catch-all except).
- Tier: code-writer (sonnet); no sensitive paths.

---

## Phase 1 — campaign visibility in Ask LINDA (one PR)

Goal: "What have the responses to our most recent email campaign been?" gets a
real answer for **both** campaign kinds, inside the `max_loops = 5` budget.

### 1.1 Extract shared campaign stats service (pure refactor + additions)

New module `backend/app/services/campaign_stats.py` (async, mirrors the router
call convention `(db: AsyncSession, tenant: Tenant, ...)`):

- `list_campaigns(db, tenant, kind=None, status=None, limit=10) -> List[Dict]` —
  both kinds, ordered by `COALESCE(started_at, created_at) DESC`; each row:
  id, name, kind, status, channel, subject, sent_count, started_at, ended_at.
  This is the "most recent campaign" resolver.
- `compute_rollup(db, tenant, campaign_id) -> Dict` — body moved verbatim from
  `api/campaigns.py:282-347`, returning a plain dict.
  `backend/app/api/campaigns.py` keeps the `CampaignRollup` Pydantic model
  (`:106`) and its endpoint, but constructs it from the service dict
  (`CampaignRollup(**await campaign_stats.compute_rollup(...))`). No API change.
- `member_states(db, campaign_id) -> Dict[str, int]` and
  `quota_state(db, tenant_id, campaign) -> Optional[Dict]` — moved from
  `api/outreach.py:434-479`; `_campaign_out` (`outreach.py:482-499`) delegates.
  Watch the imports `quota_state` drags along (`parse_config` from
  `services/outreach/common.py`, `get_settings`, `EmailSend`) — keep the
  `local_day_bounds_utc` import function-local as it is today to avoid cycles.
- `list_campaign_replies(db, tenant, campaign_id, limit=10) -> Dict` — new:
  attributed inbound `Interaction` rows (`Interaction.campaign_id == campaign_id,
  direction == "inbound"`; attribution guaranteed by
  `email_ingest/ingest.py:452-493`) with per-reply sentiment_score, subject/
  summary snippet, occurred_at, contact; plus the raw
  `CampaignEvent(event_type="reply")` count so external-kind campaigns whose
  ESP pushed reply events without ingested interaction bodies still report a
  number. Output labels both ("attributed_replies" list vs "reply_events" count)
  so the model can explain gaps honestly.
- `campaign_overview(db, tenant, campaign_id) -> Dict` — composition used by the
  chat tool: campaign header + `compute_rollup` always + (`member_states`,
  `quota_state`) only when `kind == "outreach"`, with an explicit
  `"kind": "external"|"outreach"` field.

Both-kinds contract (this resolves the kind ambiguity explicitly):

| Metric | external | outreach |
|---|---|---|
| rollup (sent/opens/clicks/replies/bounces/unsubs/conversions/sentiment) | yes | yes (same tables) |
| member funnel (`member_states`) | n/a (no `outreach_members`) | yes |
| daily quota (`quota_state`) | n/a | yes |
| "responses" answer | reply events + attributed inbound interactions | replied member state + attributed inbound interactions |

### 1.2 Three new chat read tools

In `backend/app/services/linda_agent.py`:

- Append to `TOOLS` (`:131`):
  - `list_campaigns` — "List the tenant's email campaigns (both externally-run
    ESP campaigns and LINDA-sent outreach campaigns), most recent first. Use
    this FIRST when the user says 'our latest/most recent campaign' without
    naming one." Inputs: `kind` (optional: external|outreach), `status`
    (optional), `limit` (default 10).
  - `get_campaign_stats` — overview for one campaign (input `campaign_id`);
    description must state it returns funnel+quota only for outreach kind.
  - `list_campaign_replies` — replies/responses for one campaign (inputs
    `campaign_id`, `limit`); description points at it for "what have the
    responses been".
- Add the three names to `READ_TOOLS` (`:311`) and three executors
  `_exec_list_campaigns` / `_exec_get_campaign_stats` /
  `_exec_list_campaign_replies` that validate `campaign_id` as UUID (mirror
  `_exec_get_interaction_detail`'s error shape, `:394-398`), call
  `campaign_stats`, and wire them into `dispatch_tool` (`:615-629`).
- Executors keep tenant scoping explicit (`Campaign.tenant_id == ctx.tenant.id`)
  even though RLS also applies — same belt-and-suspenders as every existing
  executor.

Loop-budget note: "most recent campaign responses" costs `list_campaigns` (1) +
`list_campaign_replies` (1), optionally `get_campaign_stats` (1) — 3 of 5 loops
worst case. To keep it at ≤3, `list_campaigns` output should include enough
header data (sent_count, status, kind) that the model rarely needs
`get_campaign_stats` just to disambiguate.

### 1.3 System prompt update

Extend `PRODUCT_KNOWLEDGE` (`linda_agent.py:77-103`):

- Add to the surfaces list: "- Campaigns: two kinds — external ESP campaigns
  tracked for analytics, and LINDA-sent cold-outreach campaigns (drafted,
  throttled, and sent by LINDA)".
- Add a "## Campaign visibility — important" paragraph mirroring the existing
  email-visibility one (`:96-102`): never claim you can't see campaigns; for
  "most recent campaign" call `list_campaigns` first, then drill in with
  `get_campaign_stats` / `list_campaign_replies`; explain the external-vs-
  outreach distinction when relevant; only say there are no campaigns if
  `list_campaigns` returns an empty list.
- This block is inside the cached static system prompt (`build_system_blocks`,
  `:106-126`) — no structural change, cache re-warms on deploy.

### 1.4 Feature gating

Nothing to add: the new tools are only reachable through the chat endpoints,
which already enforce `require_feature("ask_linda")` (`chat.py:224`) and
white-label 404 (`chat.py:35-37,214-215`). The underlying campaign data has no
tighter flag at the REST layer (`main.py:215-216`), so chat exposes nothing REST
doesn't. State this explicitly in the spec so code-writer doesn't invent a gate.

### 1.5 Tests

- `tests/test_campaign_stats_service.py` — rollup parity with the old router
  helper (seed CampaignEvents incl. a suspected_bot click; assert
  unique-click filtering survives the move), member_states/quota_state parity,
  `list_campaigns` ordering (started_at vs created_at fallback), both-kinds
  shape of `campaign_overview`, `list_campaign_replies` for (a) attributed
  interactions, (b) reply-events-only external campaigns.
- Extend `tests/test_linda_agent_tools.py` (from Phase 0) — dispatch of the
  three new tools, invalid/foreign `campaign_id` → `{"error": ...}` not an
  exception, cross-tenant campaign id returns not-found.
- Existing suites that must stay green (refactor safety):
  `tests/test_outreach_api.py`, `tests/test_campaign_event_schema.py`,
  `tests/test_chat.py`.
- Run `python3 -c "from backend.app.main import app"` (CLAUDE.md import-time
  rule) since routers change imports.

### 1.6 Authoring & sequencing

| Step | Files | Tier |
|---|---|---|
| Spec for 1.1–1.5 | `docs/specs/campaign-visibility-chat.md` | spec-writer (opus) |
| Service extraction + router delegation | `backend/app/services/campaign_stats.py`, `backend/app/api/campaigns.py`, `backend/app/api/outreach.py` | code-writer (sonnet) |
| Chat tools + prompt | `backend/app/services/linda_agent.py` | code-writer (sonnet) |
| Tests | `tests/test_campaign_stats_service.py`, `tests/test_linda_agent_tools.py` | code-writer (sonnet) |

No sensitive paths in Phase 1 (no models.py schema change, no migration, no
rls/auth/stripe/fly/CI files). One PR; deployable independently.

### 1.7 Risks / open questions (Phase 1)

- **Role visibility:** REST campaign endpoints are tenant-principal-gated but not
  role-gated; the chat tools inherit that. Open question for product: should
  reps see quota/funnel data in chat, or is that manager-only? Default: match
  REST (no role gate).
- **Token size:** rollup + funnel dicts are small; `list_campaign_replies` must
  cap snippet length (~300 chars) like other tool outputs to protect the
  context window.
- **`sent` semantics differ subtly** between `CampaignRecipient` count
  (rollup) and `Campaign.sent_count` (header) for outreach mid-flight; tool
  output should include both labeled, and the spec should say which one the
  prompt tells the model to quote (recommend recipient count, matching the REST
  rollup).

---

## Phase 2 — proactive campaign monitor (one PR + one fable-authored migration)

Goal: periodic health checks over active campaigns, alerts with Haiku-rendered
summaries + next-step suggestions, and a wrap-up report when a campaign
completes.

### 2.0 Delivery-path decision (recommendation: ManagerAlert-style fanout)

Options considered:

1. **ManagerAlert + existing fanout** (recommended). Reuses: fingerprint dedup
   partial unique index (`ux_manager_alerts_active_fingerprint`), in-app
   Notification via the **already-CHECK-valid** `NotificationKind.MANAGER_ALERT`
   (`manager_alert_fanout.py:112-131`) so **no notifications-table migration**,
   Slack delivery with per-tenant severity gates (`AlertChannelConfig`), the
   manager portal alert feed, and the auto-resolver
   (`manager_anomaly_resolve`, `tasks.py:5853+`). Cost: one migration extending
   `ck_manager_alerts_kind` (sensitive; and it must reconcile the drift noted
   above).
2. New `NotificationKind` campaign kinds — direct bell notifications. Rejected:
   still needs a sensitive migration (`ck_notifications_kind`), and buys less —
   no dedup, no severity gating, no Slack, no feed, no resolver.
3. Digest-only (vocabulary_digest-style weekly Slack). Rejected as the primary
   path: too slow for bounce spikes/quota starvation; fine as a later additive.

### 2.1 Migration: extend `ck_manager_alerts_kind` — SENSITIVE, fable-authored

- File: new `backend/alembic/versions/cmp_001_campaign_alert_kinds.py`.
- **Authored at the fable tier directly** (alembic/versions is on the
  sensitive-path list; spec-writer/code-writer must refuse it).
- Content: drop + recreate `ck_manager_alerts_kind` with the union of (a)
  whatever the live DB actually enforces (verify first — see Discovered drift),
  (b) every kind `anomaly_detector.py` emits today, and (c) the new campaign
  kinds: `campaign_bounce_spike`, `campaign_optout_spike`,
  `campaign_no_engagement`, `campaign_stalled`, `campaign_quota_starved`,
  `campaign_completed_summary`.
- Expand/contract: this is **expand-only** (widening a CHECK). Old code never
  writes the new kinds and the widened CHECK accepts all old kinds, so it is
  safe under the Fly release flow (release_command migrates before new code
  boots while old code still serves). It can ship in the same release as the
  monitor code, but land the migration commit **first** in the PR history so a
  partial rollback never leaves code writing kinds the CHECK rejects.
  Downgrade must be guarded like `sen_001`'s (only safe with no new-kind rows).
- No new tables → **no RLS registration and no `tests/test_rls_scoping_guard.py`
  change** (state this in the spec so nobody adds one speculatively). No
  `models.py` change either — `ManagerAlert.kind` is already `String(48)`
  (`models.py:3243`); only the docstring comment listing kinds gets updated,
  which fable should do in the same commit as the migration to keep the
  vocabulary comment truthful.

### 2.2 Detector service (deterministic, no LLM)

New module `backend/app/services/campaign_monitor.py`, **sync** (Celery-side,
mirroring `anomaly_detector.py` / `campaign_winner_service.py`):

- `scan_all_tenants(session) -> Dict` — iterate `session.query(Tenant).all()`,
  `with tenant_context(tenant.id, session):`, per-tenant try/except with
  per-tenant commit + rollback isolation (copy the pattern at
  `tenant_insights_service.py:333-343`). Cheap fast-skip: one
  `SELECT count(*) FROM campaigns WHERE status='active' OR ended_at > now()-interval '2 days'`
  per tenant before doing anything else.
- Per active campaign, compute from `compute_rollup`-equivalent SQL (sync
  session — reimplement the three aggregate queries locally in this module or
  add sync variants in `campaign_stats.py`; **open question** for spec-writer:
  prefer sharing SQL text over maintaining async+sync twins — recommend small
  private sync helpers in `campaign_monitor.py` with a comment pointing at
  `campaign_stats.py` as the source of truth for definitions):
  - `campaign_bounce_spike`: bounces/sent ≥ 5% with sent ≥ 20. Severity high
    (deliverability damage compounds).
  - `campaign_optout_spike`: (unsubscribes + opted-out members)/sent ≥ 2%,
    sent ≥ 20. Severity high.
  - `campaign_no_engagement`: sent ≥ 30 (reuses the winner-service ≥30-sends
    precedent) and zero replies AND (for external with open tracking) open rate
    < 10%. Severity medium.
  - `campaign_stalled` (outreach only): status `active`, pending/approved
    members > 0, but no `EmailSend` rows in the last 3 days. Severity medium.
  - `campaign_quota_starved` (outreach only): from `quota_state` inputs —
    campaign sent 0 today while `tenant_sent_today >=
    OUTREACH_TENANT_DAILY_SEND_CAP`, persisting across 2 consecutive scans
    (encode "2 consecutive" via the fingerprint window, below). Severity low.
- Thresholds are module-level constants with a doc comment saying they're
  provisional pending two weeks of real traffic (same posture as the anomaly
  scan's cadence comment, `tasks.py:421-424`). Do **not** put them in
  AlertChannelConfig yet — that would be another migration.
- Dedup: `_fingerprint(kind, str(campaign_id))` inserted through the same
  guarded-insert idiom as `anomaly_detector.py:1138-1177` — the partial unique
  index means a firing condition won't re-alert until the previous alert
  resolves. Add campaign-kind awareness to the auto-resolver
  (`anomaly_detector.resolve_stale` / `manager_anomaly_resolve`) or give
  campaign alerts a simple time-based resolution (resolve when the campaign
  completes or the condition clears on a later scan); recommend the latter,
  implemented inside `campaign_monitor.py` so `anomaly_detector.py` is untouched.
- `domain` column: set `"sales"` (campaigns are a sales motion; the Slack
  per-domain channel map at `manager_alert_fanout.py:140-156` then routes them
  like other sales alerts).

### 2.3 Haiku rendering of titles/suggestions (Layer A compliant)

- After detection, render `title` + `body` (2-3 sentences: what happened, why it
  matters, 1-2 concrete suggestions — e.g. "pause and rework subject line",
  "bounce rate suggests a stale list segment; re-verify before resuming") with
  **one Haiku call per alert** through ModelRouter:
  `forced_tier=Tier.HAIKU`, `call_site="campaign_monitor"`, model id only via
  `model_catalog.HAIKU` — copy the shape at
  `manager_recommendation_builder.py:52,397`. Never a hardcoded model string
  (`tests/test_model_catalog.py` guard).
- **Deterministic fallback:** if the LLM call fails, insert the alert anyway
  with a template title/body from the evidence dict. Detection and delivery
  must never depend on LLM availability.
- Suggestions stay advisory text in v1 — no auto-created WriteProposals from a
  beat task (proposals are a chat-session concept, `linda_agent.py:1-8`).

### 2.4 Completion wrap-up report

- Trigger: detected **in the scan** (not by hooking the webhook emitters):
  campaign with `status='completed'` or `ended_at` within the lookback and no
  `insights["completion_report"]` key. This covers both kinds — outreach
  completion set by the scheduler (which also fires the external
  `campaign.completed` webhook, `scheduler.py:784`), external campaigns via
  `ended_at` from the ingest API — and is idempotent by construction.
- Action: compute final rollup + funnel, store the structured report into
  `Campaign.insights` JSONB (`models.py:2442` — existing column, **no
  migration**), and emit one `campaign_completed_summary` ManagerAlert
  (severity low; fingerprint `("campaign_completed_summary", campaign_id)`)
  whose body is the Haiku-rendered narrative ("Campaign X finished: 240 sent,
  18 replies (7.5%), sentiment +0.3; best-engaged segment ...; suggestions for
  the next run ...").
- Chat benefit: once the report is in `Campaign.insights`, Phase 1's
  `get_campaign_stats` can include it for free — spec should have the tool
  surface `insights.completion_report` when present.

### 2.5 Beat task + cadence

- `backend/app/tasks.py`: task `campaign_monitor_scan_all_tenants` (thin wrapper
  like `tasks.py:5779-5823`: `_get_sync_session()`, call
  `campaign_monitor.scan_all_tenants`, then re-load alerts created in the last
  60s per tenant under `tenant_context` and call `manager_alert_fanout.fanout`
  — reuse the exact fanout reload idiom) + beat entry.
- **Cadence: hourly** (`crontab(minute=40)` — offset from the :15/:25/:30 herd).
  Justification: outreach sends are day-granular (send windows + daily
  throttles, `tasks.py:312-314`), so 15-minute scanning buys nothing except
  noise; but bounce spikes mid-send-window deserve same-hour detection, so
  daily is too slow. Fingerprint dedup makes the marginal cost of hourly ≈ a
  few aggregate queries per active campaign. Same "dial it after two weeks of
  traffic" note as the anomaly scan.
- Route to `{"queue": "batch"}` in task_routes like other sweeps
  (`tasks.py:202`).
- `tasks.py` is **not** on the sensitive list — code-writer may edit it.

### 2.6 Feature gating / tenant scoping for the monitor

- The monitor keys off **data existence** (tenant has campaigns), not the
  `ask_linda` flag — it's a manager-surface feature like the anomaly scan,
  which runs for every tenant unconditionally. No plan-flag check in v1.
- White-label tenants: alerts themselves are fine (manager alerts already flow
  to white-label tenants); the **Haiku prompt must not brand the copy as
  "LINDA suggests"** — keep rendering product-neutral ("Suggestion: ..."), same
  reason the chat surface 404s for white-label. Cheap to enforce in the prompt
  template; note it in the spec.
- RLS: all reads/writes happen inside `tenant_context` per tenant; the
  fast-skip count query also runs inside the context (fail-closed means an
  unscoped query would silently see zero rows — the scan must never rely on
  unscoped reads; see the comment at `tasks.py:5801-5802`).

### 2.7 Tests

- `tests/test_campaign_monitor.py`:
  - each detector: below-threshold → no alert; above → one alert with expected
    kind/severity/evidence;
  - idempotency: run scan twice → exactly one active alert per condition
    (fingerprint dedup);
  - condition-cleared → alert resolved on next scan; re-fire allowed after;
  - completion report: written once into `Campaign.insights`, alert emitted
    once, second scan no-ops;
  - LLM-failure path: stub router to raise → alert still inserted with
    template body;
  - tenant isolation: two tenants, alert rows land under the right tenant, one
    tenant's exception doesn't block the other (per-tenant commit isolation);
  - both kinds: external campaign triggers bounce/engagement detectors;
    outreach-only detectors skip external campaigns.
- Migration: extend whatever pattern `tests/test_manager_anomaly_detector.py`
  uses for kind vocabulary, asserting every kind `campaign_monitor` emits is in
  the migration's CHECK list (guards the drift class we just discovered).
- Explicitly: **no `tests/test_rls_scoping_guard.py` change** (no new tables).

### 2.8 Authoring & sequencing (Phase 2)

| Step | Files | Tier |
|---|---|---|
| Verify live `ck_manager_alerts_kind` state; author migration + `models.py` kind-comment update | `backend/alembic/versions/cmp_001_campaign_alert_kinds.py`, comment-only touch in `backend/app/models.py:3240-3243` | **fable directly** (sensitive path) |
| Spec for detectors/rendering/beat/tests | `docs/specs/campaign-monitor.md` | spec-writer (opus) — spec must mark the migration as out of its scope, already handled |
| Detector service + rendering + wrap-up | `backend/app/services/campaign_monitor.py` | code-writer (sonnet) |
| Beat task + routing | `backend/app/tasks.py` | code-writer (sonnet) |
| Tests | `tests/test_campaign_monitor.py` | code-writer (sonnet) |
| PR review incl. migration-safety checklist | — | code-reviewer (fable) |

Order inside the PR: migration commit first, then service/task/tests. Phase 2
depends on Phase 1 only for the `campaign_stats.py` metric definitions (and not
at import time if the sync-helper recommendation in 2.2 is taken); it can ship
in the following release.

### 2.9 Risks / open questions (Phase 2)

- **The `ck_manager_alerts_kind` drift** (top of doc) — must be resolved by the
  fable migration author before anything else; it may mean staging already has
  failing CS/Support alert inserts (worth an independent bug ticket).
- Threshold values are guesses; product should sign off. Constants are
  deliberately cheap to change.
- Manager-portal alert feed rendering: new kinds will appear with whatever
  generic rendering the portal has for unknown kinds — verify the frontend
  doesn't switch on a closed kind list (open question for spec-writer to have
  code-scout check `apps/app` alert components).
- Haiku volume: bounded at (active campaigns with a *newly firing* condition)
  per hour — dedup keeps this near zero steady-state. No batching needed in v1.
- Sync-vs-async duplication of rollup SQL (2.2) — accepted, but flag if
  `campaign_stats.py` definitions change later; the comment cross-link is the
  mitigation.

---

## Phase 3 — later enhancements (not in the first two PRs)

1. **A/B-informed suggestions:** enrich alert/wrap-up suggestions with
   `Experiment` conclusions from `campaign_winner_service.py` ("variant B's
   subject won its test — consider it for the next run"). Deterministic join on
   the existing Experiment rows; Haiku only phrases it. Files:
   `campaign_monitor.py` only. Tier: spec-writer → code-writer.
2. **Send-time analysis:** open/click timestamps from `CampaignEvent` bucketed
   by local hour/day → "your replies cluster Tue-Thu mornings" in wrap-up
   reports. Pure SQL + Haiku phrasing.
3. **Chat "subscribe me to campaign updates":** per-user subscription needs
   state. Options: (a) new table (`campaign_subscriptions`) — requires
   migration + automatic RLS scoping + `tests/test_rls_scoping_guard.py` update
   → fable-authored schema step; or (b) piggyback on an existing per-user
   preferences JSONB if one exists (code-scout task to confirm). Then a new
   draft tool (`propose_campaign_subscription`) through the existing
   WriteProposal confirm flow (`chat.py:319-612`) and a delivery hook in the
   monitor fanout. Defer until Phase 2 alert quality is validated.
4. **Weekly campaign digest** (vocabulary_digest-style Slack digest,
   `tasks.py:377`, `digest_service`) summarizing all active campaigns — additive
   delivery channel once thresholds are tuned.
5. **Copy-drafting suggestions** ("want me to draft a fresh bump email?") —
   this crosses into Sonnet-tier drafting and WriteProposals; route through chat
   (user-initiated) rather than the beat task.

---

## Summary of tier assignments (per CLAUDE.md routing)

- **Fable directly:** Phase 2 migration `cmp_001_campaign_alert_kinds.py` +
  the drift verification + `models.py` comment touch; final code review of both
  PRs (code-reviewer); this plan.
- **spec-writer (opus):** `docs/specs/campaign-visibility-chat.md`,
  `docs/specs/campaign-monitor.md` (excluding the migration).
- **code-writer (sonnet):** `campaign_stats.py`, `linda_agent.py` tools/prompt,
  router delegation edits, `campaign_monitor.py`, `tasks.py` beat entry, all
  tests.
- **code-scout (haiku):** pre-spec lookups (frontend alert-kind rendering,
  existence of a per-user preferences JSONB for Phase 3).

Layer A compliance: the only new runtime LLM usage is Haiku rendering via
`model_catalog.HAIKU` + ModelRouter (`forced_tier=Tier.HAIKU`) with the
deterministic fallback; chat continues on `model_catalog.SONNET`. No new model
ids anywhere.
