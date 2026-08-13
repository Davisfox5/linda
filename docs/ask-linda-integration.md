# Ask LINDA — integration contract

What an external console needs in order to drive and observe the Ask LINDA
agent: the SSE frame shapes streamed by `/chat`, and the manager
recommendation categories with the artifact each one creates on apply.

Implementation: `backend/app/api/chat.py`,
`backend/app/services/linda_agent.py`, `backend/app/api/manager.py`.
Webhook delivery is a separate contract — see [webhooks.md](webhooks.md).

---

## 1. `/chat` SSE frames

`POST /api/v1/chat` streams `text/event-stream`. Every frame is a single
`data:` line containing a JSON object with a `type` discriminator. There are
no other line kinds except periodic `: keep-alive` comments, which carry no
payload and should be ignored.

Access is gated by `require_feature("ask_linda")` — a tenant whose plan
lacks it gets `402` before the stream opens, not an error frame.

### Frame catalogue

| `type` | Fields | Meaning |
|---|---|---|
| `conversation` | `conversation_id` | Always first. The id to reuse for follow-up turns. |
| `text` | `delta` | An incremental chunk of assistant prose. Concatenate in arrival order. |
| `tool_use` | `tool`, `input` | The agent is calling a tool. `tool` is the name, `input` the argument object. |
| `tool_result` | `tool`, `result` | That tool returned. `result` is the (context-fitted) result object. |
| `proposal` | `proposal` | A draft tool staged a write for human confirmation. |
| `error` | `message` | Turn failed. Terminal. |
| `done` | — | Turn complete. Terminal. |

```
data: {"type":"conversation","conversation_id":"3f2a...-...."}
data: {"type":"text","delta":"Looking at Acme's recent calls"}
data: {"type":"tool_use","tool":"search_interactions","input":{"query":"Acme pricing"}}
data: {"type":"tool_result","tool":"search_interactions","result":{"results":[...]}}
data: {"type":"done"}
```

### Three things that trip up parsers

**1. `delta`, `tool`, `input`, `result` — not `text`, `name`, `arguments`,
`content`.** The field names are as tabulated above; there are no aliases.

**2. Draft tools emit `proposal`, never `tool_result`.** Tools that stage a
write (`propose_action_item`, `propose_step_dispatch`, and the rest of
`DRAFT_TOOLS`) return a proposal that a human must confirm before anything
happens. Those emit a `proposal` frame *instead of* a `tool_result`. A
display that only watches `tool_use`/`tool_result` will show the agent
calling the tool and then apparently never finishing — the most confusing
possible rendering of the most consequential action the agent takes. Render
`proposal` explicitly, and treat it as "awaiting human decision".

**3. There is no correlation id on the wire.** `tool_use_id` exists
internally but is not emitted. Pair a `tool_result` to its `tool_use` by
arrival order within the turn. If you need hard pairing, ask — emitting the
id is a small change, not a redesign.

### Truncation

`tool_result.result` is passed through the context-budget seam
(`services/linda_context.py`) before it is streamed or persisted. An
oversized result may carry a `_context_note` key stating how many rows were
dropped out of how many total. **Surface that note if you display row
counts** — otherwise a UI showing 3 of 40 results reads as "there were 3".
Ids are never truncated; prose is what gets sacrificed.

---

## 2. Manager recommendation categories

`POST /api/v1/manager/recommendations/{id}/apply` dispatches on
`recommendation.category` and creates one concrete artifact. It requires the
`manager` role, and 409s if the recommendation is not `open`. An unknown
category is a `400` — the endpoint fails closed.

### Auto-apply safety

Exactly one category produces an artifact that can reach a prospect:

> **`run_campaign` creates a `Campaign` row.** Never auto-apply it.

Everything else creates an internal artifact — a coaching note, an action
item, a KB request, or a playbook entry — visible only to the tenant's own
staff. Note that `outreach_at_risk_customer` and its siblings are internal
*despite the name*: they create an `ActionItem` telling a human to reach
out, not an outbound message.

The campaign `run_campaign` creates is a **draft**: named `[Draft] …`, with
no members enrolled and nothing scheduled. Sending still requires
enrollment and approval. It is nonetheless the wrong thing to create without
a human deciding to, which is why it stays off any allowlist.

### Full category table

| Category | Artifact | Prospect-facing? |
|---|---|---|
| `coach_rep` | `CoachingNote` | No |
| `run_campaign` | **`Campaign`** (draft) | **Yes — never auto-apply** |
| `outreach_at_risk_customer` | `ActionItem` | No |
| `promote_winning_script` | Playbook entry on `Tenant.tenant_context` | No |
| `coach_csm` | `CoachingNote` | No |
| `schedule_qbr` | `ActionItem` | No |
| `flag_renewal_risk` | `ActionItem` | No |
| `assign_expansion_play` | `ActionItem` | No |
| `coach_support_agent` | `CoachingNote` | No |
| `update_kb_article` | `KBArticleRequest` | No |
| `route_to_specialist` | `ActionItem` | No |
| `escalate_recurring_issue` | `KBArticleRequest` | No |
| `prevent_no_touch_churn` | `ActionItem` | No |
| `prevent_lead_stall` | `ActionItem` | No |
| `proactive_outreach_repeat_support` | `ActionItem` | No |
| `address_recurring_issue` | `KBArticleRequest` | No |

`_apply_outreach` (every `ActionItem` row above) requires
`target.customer_id` and 422s without it; it anchors the item to that
customer's most recent interaction and 422s if the customer has none.

**This table is the allowlist source of truth.** If a new category appears
that is not listed here, treat it as prospect-facing until this document
says otherwise — the failure mode of guessing wrong in the other direction
is an unrequested email to a real business.
