# Native video-in-UI — future phase, deliberately deferred

**Status: not started, not scheduled.** This is a scoping note for a
capability LINDA does not have and is not building this release:
rendering the meeting's **video** natively inside LINDA's own UI,
alongside the live transcript/coaching/alert panel documented in
[`docs/api/live-transcription.md`](../api/live-transcription.md).

Today (this release), LINDA's live pipeline is audio-in →
analysis-out. Nothing in the codebase decodes, relays, or renders video
frames — `backend/app/api/telephony.py`, `audiohook.py`, and the
`MeetingBotJob` schema landing this release
(`backend/app/models.py`, `backend/alembic/versions/live_001_live_api_meeting_bots.py`)
all move audio only. This doc is about whether and when to change that.

## What meeting-bot vendors expose

The universal meeting-bot connector landing this release
(`MeetingBotJob.provider`, defaulting to `"recall"`) dispatches a
third-party bot into a Zoom/Meet/Teams meeting as a participant. Bot
vendors in this space typically expose two tiers of media access beyond
the recording/audio file LINDA consumes today:

- **Real-time audio/video media streams** — a WebSocket or WebRTC feed
  of the meeting's mixed or per-participant audio *and* video, pushed to
  a URL the caller supplies at bot-dispatch time. This is the same
  general shape as the audio-only live paths LINDA already has (Twilio
  Media Streams, Genesys AudioHook) but with a video track added.
- **A hosted, embeddable live view** — some vendors offer their own
  ready-made "watch the meeting" web widget (an iframe you drop into
  your product) instead of, or in addition to, raw media streams —
  closer to how `docs/api/live-transcription.md`'s embeddable widget
  works for LINDA's own transcript/coaching view.

Either path is a vendor capability LINDA could opt into; neither is
wired up today.

## What it would take

**Media relay infrastructure.** Audio alone already needed the
hardening documented in
[`docs/complexity/02-realtime-media.md`](../complexity/02-realtime-media.md)
— bounded queues, backpressure, cross-thread synchronization, grace-period
reconnect handling — for a payload that's kilobits/sec. Video is easily
two orders of magnitude more bandwidth per stream, and unlike audio it
can't be dropped-and-continued as gracefully: a stalled video pane is
immediately visible to the user in a way a half-second transcript delay
isn't. Relaying it (rather than just pointing the browser at the
vendor's own hosted view) means a media server in the request path,
which is new operational surface LINDA doesn't run today — nothing in
`fly.toml`'s `[processes]` today is a media relay.

**Latency.** The live pipeline's existing budget (~3s coaching cadence,
sub-second transcript display — see `02-realtime-media.md` §1) is tuned
for audio-derived signals. A video pane sitting next to live coaching
sets a much tighter, more visible latency bar: viewers notice audio/video
sync drift and stalls far more readily than they notice a coaching hint
arriving a second late.

**Cost.** Bot-vendor real-time video streaming and any LINDA-side relay
both bill by stream-minute or bandwidth, on top of the per-minute costs
the plan catalog already caps (`max_monthly_minutes` in
`backend/app/plans.py`). Video would meaningfully change the unit
economics of a live session for no proportional increase in the
transcript/coaching/sentiment/KB value LINDA actually sells today — all
of which are audio/text-derived, not video-derived.

## ToS / consent considerations

Video capture is not "audio capture plus a picture" from a compliance
standpoint — it typically triggers stricter consent/notification
obligations than audio-only transcription in a number of jurisdictions
(some U.S. states and several countries distinguish audio-recording
consent from video/image capture, and workplace-monitoring rules often
treat visual recording of employees more strictly than voice). LINDA's
existing consent handling — the `consent_attestation` flag tenants set
on a SIPREC integration (`SiprecAdminConfigIn` in
`backend/app/api/siprec.py`) and the "this call may be transcribed"
greeting `telephony.py` injects into the Twilio TwiML when
`pii_redaction_enabled` is set — is scoped to audio recording. None of
it has been reviewed against video capture, and each meeting platform's
own bot/recording terms (Zoom, Google Meet, Microsoft Teams) separately
constrain what a third-party bot may capture, store, and for how long.
Any video work needs its own per-platform ToS and consent review before
implementation starts, not a re-use of the audio-path attestation.

## Phased recommendation

- **Phase 1 — shipping now: audio + analysis.** The live pipeline
  (transcript, coaching, alerts, sentiment, KB retrieval — see
  `docs/api/live-transcription.md`), fed by the meeting-bot connector
  for native Zoom/Meet/Teams apps and the browser-extension listener
  (`apps/extension/`) for meetings running in a browser tab — see
  `docs/integrations/meeting-platforms.md`. Audio only, no video, on
  either path.

- **Phase 2 — vendor real-time video pane.** If customer demand
  materializes, the cheapest next step is *not* building a media relay:
  point the browser at the meeting-bot vendor's own hosted live-view
  widget (or its client-side real-time stream, rendered client-side) next
  to LINDA's existing analysis panel, so LINDA never touches the video
  bytes server-side. This avoids the relay-infrastructure cost above
  entirely and is the natural next increment once Phase 1 is stable.

- **Phase 3 — native SDK evaluation.** Only revisit building a real
  LINDA-hosted video pipeline (ingesting and relaying raw vendor
  video/WebRTC streams, or evaluating platform-native SDKs like Zoom's
  Video SDK) if Phase 2's "point at the vendor's widget" approach proves
  insufficient for a concrete, funded use case. This is the expensive
  path — full media-relay infrastructure, per-platform ToS
  certification, and the latency/cost profile above — and should not be
  started speculatively.

## Why this is deferred, plainly

LINDA's analytical value — transcript, coaching hints, sentiment,
KB retrieval, alerts — is derived entirely from audio and text today.
None of the existing analysis (`LiveCoachingService`,
`LFTriggerScanner`, `RetrievalService`, paralinguistics) reads a video
frame, and there's no roadmap item that would. Building heavy media
infrastructure to render video pixels next to that analysis buys very
little analytical gain for a real increase in latency risk, cost, and
compliance surface. Phase 1 ships the analysis; Phase 2 is the cheap
version of "customers also want to see the person's face while they read
the coaching panel," deferred until there's evidence that's actually
wanted.
