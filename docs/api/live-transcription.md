# Live Transcription API (Enterprise)

Real-time transcript, coaching, and knowledge-base events for a call in
progress. Implementation: `backend/app/api/websocket.py` (WS handlers),
`backend/app/api/ws_tickets.py` + `backend/app/services/ws_tickets.py`
(auth handshake). This document is the external API reference for
Enterprise customers building their own live-call surface (a CTI
integration, an internal dashboard, an embedded widget) on top of LINDA's
live pipeline.

## Overview

A live call has two roles:

- **agent** — the party streaming audio in and receiving transcript +
  coaching events out. Connects to `wss://…/ws/live/{session_id}`.
- **monitor** — a read-only observer (e.g. a manager, or your own
  dashboard) that only receives events. Connects to
  `wss://…/ws/monitor/{session_id}`.

Both roles authenticate the same way: mint a short-lived **ticket** over
HTTPS (`POST /api/v1/ws/tickets`), then open the WebSocket with
`?ticket=<ticket>`. See [Ticket flow](#ticket-flow) below.

## Enterprise packaging

**(landing in this release)** Programmatic (API-key) access to the live
pipeline is gated behind the `live_transcription_api` feature flag on
the tenant — a new key in `PLANS["enterprise"].features` in
`backend/app/plans.py`, `False` on every other tier. `apply_tier()`
merges each tier's `TierSpec.features` dict onto
`Tenant.features_enabled` whenever a tenant's plan changes (sign-up,
upgrade, or the Stripe webhook syncing a subscription change), so
Enterprise tenants get it automatically — no manual flag flip. Two
sibling flags land alongside it, also Enterprise-only: `embedded_transcripts`
and `meeting_assist`.

This gate applies **only to API-key callers** (`POST /api/v1/ws/tickets`
and `GET /api/v1/live-sessions*` both check
`principal.source == "api_key"` before enforcing it). Human dashboard
sessions (Clerk/session auth) are unaffected — the in-app agent/monitor
UI is still governed by the existing `real_time_transcription` /
`live_coaching` tier flags, not this one.

API keys additionally need one of two scopes, validated against
`backend.app.auth.API_KEY_SCOPES`:

| Scope | Grants |
|---|---|
| `live:read` | `POST /api/v1/ws/tickets` with `role: "monitor"`, and `GET /api/v1/live-sessions*` — consume transcript/coaching/alert events, read-only. |
| `live:write` | `POST /api/v1/ws/tickets` with `role: "agent"` — stream your own audio in (bring-your-own-audio) and receive the same events back. |

A tenant without `live_transcription_api` gets `402`; an API key missing
the required scope gets `403: missing scope: live:read` (or
`live:write`). Route-to-scope mapping is tracked in
`docs/api_key_scope_map.yaml` alongside every other write surface.

## Ticket flow

Browsers can't attach an `Authorization` header to a `WebSocket`
constructor, so auth happens in two steps: mint a ticket over HTTPS with
your normal Bearer/API-key credential, then open the WebSocket with the
ticket as a query param. Tickets are single-use (`GETDEL` on consume),
expire in 120 seconds (`DEFAULT_TICKET_TTL_SEC`), and are bound to
`(tenant_id, session_id, role)` — a ticket minted for one session or role
cannot open a socket for another.

### `POST /api/v1/ws/tickets`

Request body (`TicketRequest`):

```json
{
  "session_id": "s-a1b2c3d4e5f6a7b8",
  "role": "agent",
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

- `session_id` — optional. For `role: "monitor"` (or when omitted with a
  non-API-key `role: "agent"` caller), the server just generates
  `s-<16 hex chars>`. **(landing in this release)** For an API-key
  `role: "agent"` ticket with no `session_id` — the bring-your-own-audio
  case — the server persists a real `LiveSession` row up front (so the
  eventual transcript has somewhere to land) and fires
  `live_session.started` immediately with `external_call_id: null`.
- `role` — `"agent"` or `"monitor"`.
- `user_id` — **required** when `role: "monitor"`, *except* for API-key
  callers: tenant API keys are tenant-admin scoped and carry no
  individual user, so a `live:read`-scoped key mints monitor tickets
  without one. For everyone else, the server looks the user up on the
  tenant and requires `manager` or `admin` (`_MONITOR_ROLES`) before
  issuing the ticket. Optional for `role: "agent"`, where it's just
  recorded for audit.

Response `201` (`TicketResponse`):

```json
{
  "ticket": "kx7F3n9q...(43-char urlsafe token)",
  "session_id": "s-a1b2c3d4e5f6a7b8",
  "role": "agent",
  "expires_at": 1755550120.42
}
```

`expires_at` is a Unix timestamp (seconds, float). Open the WebSocket
before it passes — a stale, already-consumed, or session/role-mismatched
ticket closes the connection immediately with close code `4401`. A
tenant over `MAX_CONNECTIONS_PER_MINUTE` (30/min, keyed on API-key hash)
gets `4429` instead.

### `wss://…/ws/live/{session_id}` — agent connection

Open with `?ticket=<ticket>` from a ticket minted with `role: "agent"`.

- **Send:** binary WebSocket frames of raw audio bytes — forwarded
  directly to the Deepgram live connection (no re-encoding, no
  persistence to disk/object storage). Also accepts a JSON text frame
  `{"type": "ping"}`, answered with `{"type": "pong"}`.
- **Receive:** the JSON events documented under [Event
  reference](#event-reference) below, plus any pinned KB cards rehydrated
  at connect time (`kb_answer` events with `"pinned": true`).

On disconnect (clean `stop` or dropped socket), the accumulated
transcript buffer is finalized into an `Interaction` and the batch
analysis pipeline (`process_voice_interaction`) is enqueued —
`_dispatch_batch_analysis` in `websocket.py`.

### `wss://…/ws/monitor/{session_id}` — monitor connection

Open with `?ticket=<ticket>` from a ticket minted with `role: "monitor"`.
Read-only: relays every event published on `live:{session_id}:events`
(the same Redis pub/sub channel the agent connection publishes final
transcripts, coaching, alerts, sentiment, and KB hits to). A monitor may
also send `{"type": "whisper", "from_user_id": "<uuid-or-name>",
"message": "..."}`, which is republished onto that same channel for
other subscribers.

## Event reference

Every event is a single JSON object with a `type` discriminator, sent
both to the agent socket and published to the monitor channel (unless
noted). Field names below are transcribed directly from the payload
dicts built in `backend/app/api/websocket.py` /
`live_coaching_features.py`.

### `partial`

Interim (non-final) Deepgram transcript. Fires on every interim result
while `interim_results` is enabled for the tenant.

```json
{ "type": "partial", "text": "so the issue is with our", "speaker": 1, "timestamp": 1755550121.9 }
```

### `final`

A finalized Deepgram transcript segment. Also appended to the session's
Redis transcript buffer.

```json
{ "type": "final", "text": "so the issue is with our billing cycle.", "speaker": 1, "timestamp": 1755550123.4 }
```

`speaker` is Deepgram's diarized speaker index (`0` is treated as the
agent throughout the pipeline); `null` when diarization didn't return
one.

### `coaching`

An incremental coaching hint from `LiveCoachingService.hint_incremental`,
emitted on a ~30s heartbeat or after 200 words since the last hint.

```json
{
  "type": "coaching",
  "hint": "Ask if they've seen the mid-cycle proration credit yet — it usually resolves this.",
  "source_doc_title": "Billing FAQ — mid-cycle changes",
  "confidence": 0.82
}
```

### `alert`

A deterministic (zero-LLM) trigger from `LFTriggerScanner`, computed
locally from the rolling `LiveFeatureWindow` — no model call, so it can
fire within one turn. `kind` is one of `monologue`, `cancel_intent`,
`commitment`, `filler`, `patience`, `rapport`, `monotone`, `pace`,
`stress`, `silence`; `severity` is `info | warn | alert`.

```json
{
  "type": "alert",
  "kind": "cancel_intent",
  "severity": "alert",
  "message": "Customer used cancellation language.",
  "evidence": { "phrase": "we might have to cancel" },
  "t": 1755550124.1
}
```

### `brief_alert`

Fired when the customer-brief's lifecycle signals flip from off to on
between two coaching rounds, or sentiment drops ≥2 points. `kind` is one
of `churn | upsell | escalation | advocate | sentiment_drop`. Also fans
out to any tenant webhook subscribed to `brief_alert.<kind>` (see
[webhooks.md](../webhooks.md) and `BRIEF_ALERT_EVENT_MAP`).

```json
{ "type": "brief_alert", "kind": "escalation", "message": "Caller is asking to escalate. Bring in a manager if needed." }
```

The `sentiment_drop` variant carries `from`/`to` numeric scores:

```json
{ "type": "brief_alert", "kind": "sentiment_drop", "message": "Sentiment dropped from 7.5 to 4.0", "from": 7.5, "to": 4.0 }
```

### `kb_answer`

A knowledge-base suggestion, either surfaced live off a detected caller
question (`"pinned": false`) or rehydrated from a contact's pinned cards
at connect time (`"pinned": true`).

```json
{
  "type": "kb_answer",
  "pinned": false,
  "query": "do you support SSO for the enterprise plan",
  "snippet": "Enterprise tenants can enable SAML/OIDC SSO from Settings → Security…",
  "chunk_id": "b6b6a2e2-...",
  "doc_id": "1e9e2b1a-...",
  "doc_title": "SSO setup guide",
  "source_url": "https://help.example.com/sso",
  "confidence": 0.74,
  "urgency": "normal",
  "source": "keyterm"
}
```

The pinned/rehydrated variant swaps `pin_id`/`chunk_id`/`doc_id`/etc. for
the same fields plus `"pin_id"` and drops `urgency`/`source` (see
`_load_pinned_cards`).

### `sentiment_update`

Tier-gated: only sent when `Tenant.features_enabled["live_sentiment"]`
is true (Growth and Enterprise; see `plans.py`).

```json
{ "type": "sentiment_update", "score": 6.5, "trend": "declining" }
```

### `features`

A rolling 60-second window snapshot (`LiveFeatureWindow.snapshot`),
throttled to at most once per 5 seconds and only after 3 new `final`
segments.

```json
{
  "type": "features",
  "window_sec": 60.0,
  "rep_talk_pct": 0.42,
  "customer_talk_pct": 0.51,
  "silence_pct": 0.07,
  "patience_sec": 1.2,
  "interactivity_per_min": 8.5,
  "filler_rate_per_min": 2.0,
  "question_rate_per_min": 3.0,
  "lsm_partial": 0.61,
  "back_channel_gap_sec": 4.3
}
```

## Session discovery

**(landing in this release)** — read-side endpoints in
`backend/app/api/live_sessions.py`, gated the same way as the ticket
endpoint: API-key callers need `live_transcription_api` +
`live:read`; human dashboard sessions pass through ungated. Backed by a
schema addition landing alongside it — `LiveSession.external_call_id`
(indexed) plus a `(tenant_id, status)` index
(`backend/alembic/versions/live_001_live_api_meeting_bots.py`).

- `GET /api/v1/live-sessions?state=active|all&limit=50` — the tenant's
  live sessions, `active` (statuses `"active"`/`"live"` — ingress paths
  aren't consistent about which label they use) by default.
- `GET /api/v1/live-sessions/lookup?external_call_id=…` — resolve a
  provider-side call id (Twilio/SignalWire CallSid, Telnyx
  `call_control_id`, a SIPREC `src_call_id`/recording-session id, a
  meeting-bot id) to its LINDA session. Twilio, SignalWire, Telnyx, and
  SIPREC all now populate `external_call_id` at session-creation time.
  Most-recent match wins on a redialed id. `404` if nothing matches.
- `GET /api/v1/live-sessions/{session_id}` — a single session by LINDA id.

All three return the same shape:

```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "source": "twilio",
  "status": "active",
  "external_call_id": "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "agent_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "interaction_id": null,
  "started_at": "2026-08-07T18:00:00Z",
  "ended_at": null,
  "monitor_ws_path": "/ws/monitor/3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "embed_path": "/embed/live/3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

`monitor_ws_path`/`embed_path` are relative paths, not absolute URLs —
callers already know their own API/app hosts. Neither includes a
ticket; mint one separately via `POST /api/v1/ws/tickets`.

Matching webhook events, registered alongside the existing
`brief_alert.*` events (see [webhooks.md](../webhooks.md)) and emitted
through the new `emit_live_session_event` helper
(`backend/app/services/live_session_events.py`):

- `live_session.started` — fires from every live-capable ingress
  (Twilio/SignalWire/Telnyx webhook handlers, the SIPREC bridge) the
  moment the `LiveSession` row is created. Payload:
  `{"session_id": "...", "source": "twilio", "external_call_id": "CA..."}`.
- `live_session.completed` — fires from `_dispatch_batch_analysis` once
  the transcript is persisted to its `Interaction`. Payload:
  `{"session_id": "...", "source": "twilio", "external_call_id": "CA...", "interaction_id": "..."}`.

## Embeddable widget

**(landing in this release)** — `apps/app/src/app/embed/live/[sessionId]/page.tsx`
serves a standalone, iframe-friendly monitor view (not part of the
authenticated `(app)` route group, not Clerk-gated — see
`src/middleware.ts`'s public matcher and the `frame-ancestors *` header
in `next.config.mjs` that lets third-party origins frame it):

```
{app}/embed/live/{session_id}?ticket=<monitor-ticket>&theme=light|dark&alerts=1
```

- `ticket` — **required.** A `role: "monitor"` ticket, minted
  server-side by the customer's backend (never in the customer's browser
  — minting requires the tenant's own Bearer/API-key credential, which
  must never reach the browser) and injected into the iframe `src`.
  Same single-use, 120s-TTL ticket as the raw WebSocket path — that's
  the entire security model for the embed, no separate embed-specific
  auth. Missing or invalid ticket renders a "Session unavailable"
  notice instead of connecting.
- `theme` — `light` (default) or `dark`.
- `alerts` — `1` renders a coaching/brief-alert strip above the
  transcript (latest 3); omitted or any other value shows transcript +
  status only.

The widget reuses the same WS-URL builder and event reducer
(`useLiveSession` in `apps/app/src/lib/live-coaching.ts`) as the
authenticated in-app monitor view, so the widget and the first-party app
can't drift on the wire protocol independently.

Because the ticket is single-use and short-lived, a leaked embed URL is
only live for the remainder of that one ticket's 120-second window (or
until the socket it opened disconnects) — the customer's server must
mint a fresh ticket per embed render, not cache one.

## Raw data

This API returns JSON only — LINDA does not ship a rendered transcript
or coaching UI over the wire. Rendering (transcript scroll, coaching
cards, alert toasts) is the consumer's job, or the embeddable widget's
job if you use that instead of the raw WebSocket.
