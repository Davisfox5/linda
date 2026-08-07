# LINDA Meeting Listener (Chrome extension)

A non-bot meeting listener. You run your meeting (Google Meet, Zoom Web,
Teams Web — any browser tab) normally; this extension captures that tab's
audio (optionally mixed with your microphone) and streams it into LINDA's
existing live-transcription WebSocket, so live transcription, coaching and
KB suggestions show up in LINDA's own UI. Nothing joins your call as a
participant.

## How it talks to LINDA (for reference)

Verified against `backend/app/api/websocket.py` and
`backend/app/api/ws_tickets.py` — not invented:

1. `POST {api base}/api/v1/ws/tickets`, `Authorization: Bearer <API key>`,
   body `{"role": "agent"}` → `{ ticket, session_id, role, expires_at }`
   (single-use, short-lived ticket; `session_id` is minted server-side when
   omitted).
2. `WS {api base, ws(s)://}/ws/live/{session_id}?ticket={ticket}` — ticket
   is a query param, validated before the socket is accepted; closes with
   code `4401` if it's missing/expired/already consumed.
3. Audio: binary WS frames are forwarded verbatim to Deepgram
   (`websocket.py` `dg_connection.send(data["bytes"])`). The server's
   Deepgram `LiveOptions` do **not** set `encoding` / `sample_rate` /
   `channels`, so Deepgram is relying on container auto-detection (per
   Deepgram's docs, raw/headerless PCM requires an explicit encoding —
   containerized audio, including opus-in-WebM, is auto-detected). This
   extension therefore streams a continuous `audio/webm;codecs=opus`
   `MediaRecorder` stream in ~250ms chunks, not raw PCM.
4. Clean stop: closing the WebSocket (code 1000) is what makes LINDA
   finalize the transcript and dispatch batch analysis
   (`_dispatch_batch_analysis` in `websocket.py`). The extension always
   stops the `MediaRecorder` first (flushing its last chunk) before
   closing the socket.

## Install (load unpacked)

1. Open `chrome://extensions`.
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select the `apps/extension/` directory.
4. Pin the extension (puzzle-piece icon → pin) so it's reachable from the
   toolbar.

## Configure

1. Right-click the extension icon → **Options** (or click "Open options"
   from the popup if it's not configured yet).
2. Enter your **LINDA API base URL** (e.g. `https://api.your-instance.com`
   in production, or `http://localhost:8000` in local dev).
3. Enter an **Enterprise LINDA API key with live-transcription access**
   (`csk_...`). This is a tenant-wide programmatic credential, not a
   per-user login — the extension authenticates as the key.
4. Optionally set a separate **LINDA app URL** if your frontend
   (`apps/app`) is on a different host than the API (e.g. local dev with
   the API on `:8000` and the app on `:3001`). Leave blank if LINDA is
   deployed behind a single reverse-proxied host.
5. If you'll use mic mixing, click **Test microphone access** once — this
   opens the standard Chrome mic permission prompt for the extension.
   Grant it here; the invisible background capture worker can't prompt for
   permission itself, but a permission granted from this (visible) options
   page carries over to it automatically.
6. Save. On save, the extension also requests browser permission to talk
   to your API host (`chrome.permissions.request`) — this is a one-time
   prompt so the background service worker can call the API without
   running into cross-origin restrictions.

## Use

1. Join your meeting in a normal browser tab (Google Meet, Zoom Web, Teams
   Web, etc.) and make sure that tab is the active/focused one.
2. Click the LINDA extension icon.
3. Toggle **Mix in my microphone** if you want your own voice included
   (otherwise only the tab's playback audio — i.e. the other
   participants — is captured; whether your own mic audio is already part
   of the tab's playback depends on the meeting platform).
4. Click **Start listening to this tab**. The popup shows Connecting →
   Listening.
5. Click **Open in LINDA** to watch the live transcript/coaching in
   LINDA's own UI (see the *Known gap* note below).
6. Click **Stop** when the call ends. This flushes the last audio chunk
   and closes the WebSocket cleanly so LINDA finalizes the transcript and
   runs its batch analysis pipeline.
7. If the connection drops unexpectedly, the popup shows **Disconnected**
   with a **Restart** button. Tickets are single-use, so restart mints a
   brand new ticket/session rather than resuming the old one — there is no
   automatic reconnect.

## Limitations

- **Browser-tab meetings only.** This captures a browser tab's audio via
  `chrome.tabCapture`. It cannot capture audio from native desktop apps
  (the Zoom.app, Teams.exe, Slack Huddles desktop client, etc.) — join
  those meetings in the browser instead if you want them captured.
- The captured tab must stay open and must not navigate away
  mid-call — `chrome.tabCapture` streams are tied to a specific tab.
- Requires an **Enterprise-tier LINDA API key with live access**. (Note:
  as read, `POST /api/v1/ws/tickets` itself does not enforce a plan-tier
  or scope check for `role="agent"` tickets — any valid API key can mint
  one. This requirement is a product/billing expectation, not something
  this extension or the ticket endpoint technically gates. See "Contract
  gaps" below.)
- Microphone mixing requires a one-time permission grant from the Options
  page (see Configure step 5); it can't be granted from the invisible
  capture worker.
- No auto-reconnect. Tickets are single-use by design (see
  `backend/app/services/ws_tickets.py`), so a dropped connection needs a
  manual restart, which mints a fresh ticket/session.

## Consent and recording law

Depending on your jurisdiction and company policy, transcribing/recording
a call may require the other participants' consent (some jurisdictions
require all-party consent). **You are responsible for obtaining any
consent required before starting a listening session.** This extension
does not automate consent collection or disclosure to other call
participants.

## Contract gaps found while building this (for the backend owner)

These were discovered while matching the extension to the real
`/ws/live` and `/ws/tickets` contract; none required backend changes to
work around, but they're worth knowing about:

1. **No `/live/{sessionId}` page in `apps/app`.** The popup's "Open in
   LINDA" link points at `{app URL}/live/{session_id}`, per this task's
   spec. As of this writing, `apps/app/src/app/(app)/` has no such route —
   the closest existing UI is `/coaching`, which is a *manager* monitor
   view that mints its own `role="monitor"` ticket and lets a manager pick
   which agent/session to observe, rather than a direct
   `session_id`-addressed page. Until such a route exists, the "Open in
   LINDA" link in the popup will 404 against the current frontend. This
   extension implements the link exactly as specified so it's a one-line
   fix once/if that page is added; in the meantime the closest working
   alternative is opening `/coaching` manually and picking the session
   from the list there (it does show sessions started via a ticket, since
   `useCoachingSessions` reads from the same `LiveSession`-backed store).
2. **No enforced plan-tier gate on agent-ticket minting.** The spec asks
   the Options page to note that an Enterprise API key with live access is
   required. Reading `backend/app/api/ws_tickets.py`, `POST /ws/tickets`
   with `role="agent"` only needs a valid API key resolved via
   `get_current_tenant` (`backend/app/auth.py`) — there's no scope check
   (`user_id` is only required/checked for `role="monitor"`). So today,
   any tenant's API key can mint an agent ticket and stream audio in,
   regardless of plan tier. The extension's UI copy states the
   requirement as a product expectation without claiming the server
   enforces it, since it doesn't as read.
