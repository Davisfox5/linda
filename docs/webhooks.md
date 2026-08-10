# Outbound webhooks — delivery contract

LINDA delivers events to tenant-registered webhook URLs (managed via
`/webhooks`, see `backend/app/api/webhooks.py`). This documents the wire
contract consumers verify against. Dispatcher implementation:
`backend/app/services/webhook_dispatcher.py`.

## Payload shape

Every delivery body is a JSON **object** with this envelope — enforced at
enqueue time (payloads that would violate it are rejected and logged, never
delivered):

```json
{
  "event": "customer.churned",          // always a non-empty string
  "tenant_id": "<uuid>",
  "emitted_at": "2026-07-02T18:00:00+00:00",
  "data": { ... }                        // always a JSON object
}
```

Consumers can rely on `event` being a string and `data` being an object; a
`null`, bare-array, or event-less body is a contract violation on our side.

## Headers

| Header | Meaning |
|---|---|
| `X-Linda-Event` | Event name (same as `event` in the body). |
| `X-Linda-Delivery` | Delivery row UUID — stable across retries; use it for idempotency. |
| `X-Linda-Attempt` | 1-based attempt counter. |
| `X-Linda-Timestamp` | Unix seconds at which this attempt was signed. |
| `X-Linda-Signature-V2` | **Replay-protected** signature: `t=<unix seconds>,v1=<hex>`. |

## Signature verification

1. Parse `X-Linda-Signature-V2` into `t` (integer unix seconds) and `v1` (hex).
2. Reject if `|now - t|` exceeds your tolerance. **Recommended tolerance:
   ±5 minutes** — wide enough for retries in flight and modest clock skew,
   tight enough that a captured delivery can't be replayed later.
3. Compute `HMAC_SHA256(secret, "{t}.{raw_body}")` over the **raw request
   bytes** (do not re-serialize the JSON) and compare against `v1` with a
   timing-safe comparison.

The timestamp is bound into the signed string, so an attacker who captured a
legitimate delivery cannot advance `t` without invalidating `v1`.

Each attempt (including retries) is signed fresh at send time, so retried
deliveries carry a current timestamp and pass the tolerance check.

## Legacy signature (removed)

Deliveries used to also carry `X-Linda-Signature: sha256=<hex>` with hex =
`HMAC_SHA256(secret, raw_body)`. No timestamp was bound in, so it verified
forever — i.e. it was replayable. The header was removed after known
consumers moved to v2; verify `X-Linda-Signature-V2` only.

## Retries

5 attempts with exponential backoff (10s, 1m, 5m, 30m, 2h), then the
delivery is dead-lettered. Any 2xx acknowledges a delivery. Dedupe on
`X-Linda-Delivery` if your handler is not idempotent.

## Test pings

`POST /webhooks/{id}/test` sends a `webhook.test` event carrying the same
signature headers as real deliveries, so you can verify a v2
implementation end-to-end before real traffic depends on it.

## Cold-outreach events (added with `out_001`)

Event names registered in `backend/app/services/webhook_events.py`; all
ride the standard envelope + v2 signature above. Payloads are
self-sufficient — consumers can update UI state without a follow-up
fetch. `prospect_id` is the LINDA customer UUID (prospects ARE customers).

### `outreach.email.sent`

```json
{
  "prospect_id": "<uuid>", "prospect_name": "Iron Works Gym",
  "campaign_id": "<uuid>", "campaign_name": "July gyms sweep",
  "member_id": "<uuid>",
  "step": 0,                       // 0-based touch index that was sent
  "touches_sent": 1,
  "to": "owner@irongym.com", "subject": "Quick question…",
  "email_send_id": "<uuid>", "interaction_id": "<uuid>",
  "provider": "google",
  "pipeline_status": "contacted",  // status AFTER the send
  "sent_at": "<iso8601>"
}
```

### `outreach.email.replied`

```json
{
  "prospect_id": "<uuid>", "prospect_name": "...",
  "campaign_id": "<uuid>", "campaign_name": "...",
  "member_id": "<uuid>", "interaction_id": "<uuid>",
  "from": "owner@irongym.com", "subject": "Re: …",
  "snippet": "first 500 chars of the reply body",
  "pipeline_status": "replied",
  "occurred_at": "<iso8601>", "source": "reply"
}
```

### `outreach.email.opted_out`

Same shape as `.replied` (fields may be null for manual opt-outs), plus
`source`: `"reply"` (stop-keyword reply) or `"manual"` (API/console DNC).
The prospect is `do_not_contact` from this moment and every active
sequence for them is halted.

### `outreach.link_clicked`

Fires when a prospect follows a tracked link from a campaign with
`config.track_clicks` enabled (links in the HTML part are rewritten to
`/t/{token}` redirects at send time). Every hit is emitted — repeats
included; `suspected_bot` marks hits that look like mail-gateway link
scanners (bot user-agent, or a click within seconds of delivery), so
consumers wanting "first human click" should filter on it and dedupe
per (recipient_id, url).

```json
{
  "prospect_id": "<uuid>", "prospect_name": "...",
  "campaign_id": "<uuid>", "campaign_name": "...",
  "member_id": "<uuid>", "recipient_id": "<uuid>",
  "url": "https://the-original-destination.example/pricing",
  "suspected_bot": false,
  "occurred_at": "<iso8601>"
}
```

### `outreach.email.bounced`

```json
{
  "prospect_id": "<uuid>", "campaign_id": "<uuid>",
  "campaign_name": "...", "member_id": "<uuid>",
  "to": "owner@irongym.com",
  "reason": "Delivery Status Notification (Failure)",
  "occurred_at": "<iso8601>"
}
```

### `prospect.status_changed`

```json
{
  "prospect_id": "<uuid>",
  "old_status": "queued",          // may be null (first status)
  "new_status": "contacted",       // new | queued | contacted | replied | demo | won | lost | do_not_contact
  "reason": "outreach_email_sent", // or outreach_reply / opt_out_reply / manual / …
  "campaign_id": "<uuid or null>",
  "changed_at": "<iso8601>"
}
```

Campaign-driven transitions are monotonic (never move a prospect
backwards); `manual` transitions may go anywhere.

### `campaign.completed`

```json
{
  "campaign_id": "<uuid>", "name": "July gyms sweep",
  "totals": {
    "members": 250, "sent": 412, "replied": 31,
    "bounced": 6, "opted_out": 3, "completed_no_reply": 180
  },
  "completed_at": "<iso8601>"
}
```

### `action_plan.updated`

Fired on material plan mutations (step edit / step delete); creation and
step completion keep their dedicated events.

```json
{
  "plan_id": "<uuid>", "customer_id": "<uuid or null>",
  "interaction_id": "<uuid or null>",
  "goal": "...", "status": "active", "version": 2,
  "reason": "step_edited",
  "step_id": "<uuid>", "changed_keys": ["due_date"]
}
```

### `manager_alert.created`

Fired once per alert as `manager_alert_fanout.fanout` delivers it —
anomaly scan, trend detector, commitment, concern, and campaign-monitor
alerts alike. `campaign_id` / `campaign_name` are only present when the
alert's `evidence` carries them (campaign-monitor alerts); `null` for
every other kind.

```json
{
  "alert_id": "<uuid>",
  "kind": "topic_spike",
  "severity": "high",
  "domain": "support",
  "title": "Refund mentions jumped 6x in 48 hours.",
  "body": "Baseline was 1/day; recent 12 calls.",
  "opened_at": "<iso8601>",
  "campaign_id": "<uuid or null>",
  "campaign_name": "July gyms sweep, or null"
}
```

Delivery notes: this event is not gated by `AlertChannelConfig` — a
tenant that muted in-app/Slack alert channels still receives it, since
webhook registration is itself opt-in. Deliveries are at-least-once
(retries, and rare fanout overlaps, can re-send the same alert under a
new `X-Linda-Delivery` id), so consumers should dedupe on
`data.alert_id`.
