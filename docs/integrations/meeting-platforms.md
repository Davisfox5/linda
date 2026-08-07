# Meeting & telephony platform coverage

Every channel LINDA can pull a conversation from, how it gets the audio,
and whether analysis happens live (mid-call, via the WebSocket live
pipeline — see [`docs/api/live-transcription.md`](../api/live-transcription.md))
or post-call (a recording lands after the fact and runs through the
batch pipeline). Grounded in the router registrations in
`backend/app/main.py` and the ingress modules under `backend/app/api/`.

## Coverage matrix

| Channel | Mechanism | Live / post-call | Code |
|---|---|---|---|
| Twilio | Inbound voice webhook (TwiML) + Media Streams WebSocket | **Live** | `backend/app/api/telephony.py` |
| SignalWire | Same TwiML-compatible webhook + Media Streams framing as Twilio | **Live** | `backend/app/api/telephony.py` (`signalwire_voice_webhook`, delegates to the Twilio media-stream handler) |
| Telnyx | Call Control webhook (`call.initiated`/`call.hangup`) + streaming start | **Live** | `backend/app/api/telephony.py` (`telnyx_voice_webhook`) |
| Cisco CUBE | SIPREC forking to the SRS sidecar | **Live** | `backend/app/api/siprec.py`, `backend/app/services/telephony/siprec/` |
| Avaya SBCE | SIPREC forking to the SRS sidecar | **Live** | same as above |
| Metaswitch | SIPREC forking to the SRS sidecar | **Live** | same as above |
| Genesys Cloud | AudioHook WebSocket (HMAC-signed protocol) | **Live** | `backend/app/api/audiohook.py`, `backend/app/services/telephony/audiohook/` |
| RingCentral | Recording webhook → fetch recording URL | **Post-call** | `backend/app/api/uc_telephony.py`, `backend/app/services/telephony/uc/ringcentral.py` |
| Webex Calling | Recording webhook (X-Spark-Signature) → fetch recording URL | **Post-call** | `backend/app/api/uc_telephony.py`, `backend/app/services/telephony/uc/webex.py` |
| Zoom Phone | Recording webhook (URL-validation challenge + signed events) → fetch recording URL | **Post-call** | `backend/app/api/uc_telephony.py`, `backend/app/services/telephony/uc/zoom_phone.py` |
| MiaRec | Generic ingest URL (`source: "miarec"`) | **Post-call** | `POST /api/v1/interactions/ingest-recording`, `backend/app/api/interactions.py` |
| Dubber | Generic ingest URL (`source: "dubber"`) | **Post-call** | same endpoint |
| Microsoft Teams | Compliance-recording (Graph change notifications + a certified .NET media bot) | **Post-call today; live once a bot is deployed** — control-plane code complete, requires an externally deployed certified media bot | `backend/app/api/teams_recording.py`, `backend/app/services/teams_recording/`; see [`docs/integrations/stream-3-teams/CERTIFICATION_PATH.md`](stream-3-teams/CERTIFICATION_PATH.md) |
| Zoom / Google Meet / Teams **meetings** (browser tab) | Browser-extension listener (`tabCapture`) | **Live (this release)** | `apps/extension/` |
| Zoom / Google Meet / Teams **meetings** (native app) | Universal meeting-bot vendor connector | **Live (this release)** | schema landing: `backend/app/models.py` (`MeetingBotJob`) |

## Notes on specific rows

**Metaswitch needs no meeting bot.** It's a telephony/SBC platform, not a
meeting-app vendor — it's covered exactly the way Cisco CUBE and Avaya
SBCE are: live via SIPREC forking (`SIPREC_PROVIDERS` in
`backend/app/services/telephony/siprec/__init__.py` includes
`siprec_metaswitch`) and, separately, post-call if the customer's
Metaswitch-adjacent recording system (e.g. a MiaRec/Dubber deployment
sitting in front of it) posts to the generic ingest endpoint. No bot,
certification, or vendor SDK is involved on either path.

**Consumer Skype was retired by Microsoft in May 2025.** There is no
Skype ingestion path in this codebase, and none is planned — Skype's
calling stack was folded into Teams, so the successor integration is the
Microsoft Teams compliance-recording path documented above (and, for
meetings specifically, the browser-extension / meeting-bot connector
landing this release).

## Teams compliance recording — current status

The Python control plane (subscription validation, Graph
change-notification parsing, the bot-interface abstraction) is
scaffold-complete per `backend/app/api/teams_recording.py`. What's
missing to actually record a call is entirely outside this codebase: a
certified .NET media bot (Microsoft's Calling SDK has no Python/Go/Node
binding), Azure infrastructure to host it, and Microsoft Partner Center
certification. See
[`docs/integrations/stream-3-teams/CERTIFICATION_PATH.md`](stream-3-teams/CERTIFICATION_PATH.md)
for the full workstream and status table — none of it is automatable
from inside this repo; it's a user-driven, externally-deployed
workstream.

## Meeting-app coverage landing this release

Twilio/SIPREC/AudioHook and the UC recording webhooks above cover
**telephony** — a phone call, wherever it terminates. They do not cover
a **meeting** (Zoom/Meet/Teams) that never touches a phone number or SBC.
This release adds two options for that case, both scoped to browser/app
sessions rather than the PSTN:

1. **Browser-extension listener** (`apps/extension/`, MV3 Chrome
   extension — "LINDA Meeting Listener") — uses `tabCapture` to stream
   the current tab's meeting audio (optionally mixed with the mic) into
   LINDA's live pipeline. No bot joins the call; it only listens to the
   user's own tab. A background service worker mints a ticket and hands
   off to an MV3 offscreen document (required for
   `tabCapture`/`MediaRecorder`, which service workers can't host) that
   owns the actual recorder + WebSocket, so the session survives the
   service worker being killed/restarted by Chrome.
2. **Universal meeting-bot vendor connector** — for native desktop apps
   (Zoom/Teams/Meet clients, not the browser), a third-party meeting-bot
   vendor joins the call as a participant and relays audio to LINDA. The
   lifecycle schema for this is already landing: `MeetingBotJob`
   (`backend/app/models.py`, migration
   `live_001_live_api_meeting_bots`) tracks one row per dispatched bot —
   `provider` (defaults to `"recall"`), `bot_id`, `meeting_url`,
   `platform` (`zoom | meet | teams | unknown`), and a `status` state
   machine (`requested → joining → in_call → done | failed`) — linked to
   the `LiveSession` that carries the actual transcript.

Both land this release; see the live-transcription API doc for the event
contract they feed into once connected.
