# Microsoft Teams Compliance Recording — Certification Path

This document captures the *full* path from "we have the Python control
plane" (where Stream 3 stops) to "we are a certified Microsoft Teams
Compliance Recording partner with a deployed media bot capturing real
calls". Everything below is a planning artifact — none of it is
automatable from inside Claude. Every section is a user-only workstream.

For the step-by-step execution version of this doc (exact env vars,
commands, and a verification checklist), see
[`DEPLOYMENT_RUNBOOK.md`](DEPLOYMENT_RUNBOOK.md) in this same directory.

## Status snapshot

| Component | Status | Owner |
|---|---|---|
| Python control plane (this PR) | ✅ scaffold landed | Stream 3 |
| Notification persistence (TeamsCallRecord + UcRecordingJob) | ✅ landed — `services/teams_recording/ingest.py` | Stream 3 |
| Media-bot callback contract (`/teams/bot/callback` v1) | ✅ landed — receiving half only, bot itself not built | Stream 3 |
| Subscription create/renew/delete + per-customer bootstrap | ✅ landed — `services/teams_recording/teams_graph.py`; bookkeeping lives in `Integration.provider_config`, not a new table | Stream 3 |
| Celery-beat renewal scheduling | 🟡 `renew_due_teams_subscriptions` exists; beat wiring in `tasks.py` NOT done (sensitive-path — see `DEPLOYMENT_RUNBOOK.md` §3) | Integrator |
| App-only Graph auth (MSAL) | ✅ scaffold landed; needs env vars | User |
| .NET stateful media bot | ❌ not started | User-commissioned workstream |
| Azure infrastructure (App Service / ACA) | ❌ not started | User |
| Microsoft Partner Center registration | ❌ not started | User |
| Calls.AccessMedia.All permission grant | ❌ not started | User |
| Compliance recording certification submission | ❌ not started | User |
| Customer-side PowerShell rollout | ❌ blocked on bot | Customer admin |

The scaffold gives us subscription validation, change-notification
parsing, the model, and the bot interface. None of those are sufficient
to actually record a Teams call — that requires the media bot.

## What "Teams compliance recording" actually is

A Microsoft Teams "compliance recording application" is a registered
Azure AD app that Teams's calling fabric inserts into specific users'
calls automatically, on the basis of a tenant-side
``Grant-CsTeamsComplianceRecordingPolicy`` assignment. The app is a
*media bot* — it joins the Skype Calling SDK media leg, receives mixed
or per-participant audio frames over RTP, and is responsible for
storing or relaying that audio.

Two important Microsoft constraints shape the architecture:

* **The media bot must be .NET (C#).** Microsoft's Graph Communications
  Calling SDK ships only as a .NET library. There is no Python, Go, or
  Node binding. Any media bot is therefore a separate service in your
  estate, written in C#, hosted on Windows or in a Linux container with
  the .NET runtime.
* **The bot must run within the customer's Azure tenant boundary
  for compliance certification.** Cross-tenant invitations work for
  trial/sandbox use, but Microsoft's compliance certification path
  requires the bot to be deployed in a way that meets their data-flow
  rules — typically multi-tenant Azure Container Apps with per-tenant
  isolation, or a single bot in your own AAD tenant that the customer
  consents to and grants the recording policy.

## Reference architecture (target state)

```
                     ┌──────────────────────────┐
                     │  Microsoft 365 (customer)│
                     │   ┌──────────────────┐   │
                     │   │ Teams calling    │   │
                     │   │ fabric           │   │
                     │   └──────┬───────────┘   │
                     │          │  RTP / SDP    │
                     │          ▼               │
                     │   ┌──────────────────┐   │
                     │   │ LINDA media bot  │   │  ← .NET, in Azure
                     │   │ (Graph Comms SDK)│   │
                     │   └────┬────────┬────┘   │
                     │        │        │        │
                     │        │        │ HTTPS  │
                     └────────┼────────┼────────┘
                              │        │
                Azure storage │        │ POST /teams/bot/callback
                (audio chunks)│        │
                              ▼        ▼
                         ┌────────────────────┐
                         │ LINDA backend      │  ← THIS REPO
                         │ - graph_app_auth   │
                         │ - subscriptions    │
                         │ - bot_interface    │  ← stub today
                         │ - api/teams_*      │
                         └─────────┬──────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ Transcription +    │
                         │ score pipeline     │
                         └────────────────────┘
```

The Python backend in this repo is the small grey box at the bottom.
Everything in Azure / .NET is a separate workstream.

## The certification track

Microsoft does not allow general production use of the
``Calls.AccessMedia.All`` application permission until your bot has
been certified as a compliance recording application. Certification
gates the production rollout.

Steps the user must complete (in order):

### 1. Microsoft Partner Center registration

* Sign up for a Microsoft Partner account if not already a partner:
  https://partner.microsoft.com/
* Complete the publisher verification flow (D-U-N-S, signed
  attestation). This unlocks publisher-verified consent prompts and
  is a prerequisite for compliance certification.
* Lead time: 4–8 weeks for publisher verification when not already
  enrolled.

### 2. Azure subscription + AAD tenant

* The bot needs its own AAD tenant (your own, not the customer's).
* Create an Azure subscription that will host the bot infrastructure.
* Decide on hosting: Azure App Service for Containers (simpler) vs
  Azure Container Apps (recommended for scale) vs Azure Kubernetes
  Service (most operational overhead). Microsoft's reference samples
  use App Service — start there and migrate later.

### 3. AAD app registration for the bot

* Register a multi-tenant AAD app (one app, many customer tenants).
* Add **application** permissions:
  * ``Calls.AccessMedia.All`` — required for any media bot.
  * ``Calls.JoinGroupCallAsGuest.All`` — for non-meeting calls.
  * ``OnlineMeetingArtifact.Read.All`` — to fetch the recorded artifact
    URL after a meeting completes.
* Generate a client secret (or upload a certificate — preferred for
  production).
* The values land in our env vars: ``TEAMS_BOT_APP_ID`` (client id),
  ``TEAMS_BOT_APP_SECRET`` (secret), ``TEAMS_TENANT_ID`` (your AAD
  tenant id, not the customer's).

### 4. .NET media bot implementation

* Fork or scaffold from
  https://github.com/microsoftgraph/microsoft-graph-comms-samples
  (specifically the ``HueBot`` and ``ComplianceRecordingBot`` samples).
* Implement at minimum:
  * ``IRealTimeMediaCall`` lifecycle (incoming, established,
    terminated).
  * Audio mixed-stream subscription per participant.
  * Outbound HTTPS POSTs to LINDA's ``POST /teams/bot/callback`` for
    lifecycle events (call started, participant joined, call ended).
  * Audio frame relay — typically by writing to Azure Blob Storage in
    chunked form, then notifying LINDA's callback with the blob URL.
* The bot must implement TLS endpoint termination on a public hostname
  in your Azure tenant, with a valid certificate from a public CA.
  Self-signed will not pass certification.

Estimated effort: 3–6 engineer-months for first cut, plus iteration
during certification. This is the single biggest workstream.

### 5. Microsoft certification submission

* Open a Microsoft Partner Center support case under the "Teams
  compliance recording" track.
* Submit the certification kit:
  * Demo video of the bot recording a call end-to-end.
  * Architecture diagram with data-flow / data-residency claims.
  * Security review (penetration test results, secret rotation
    process).
  * Privacy posture (GDPR DPA template, data retention defaults).
* Microsoft assigns a certification engineer; the back-and-forth is
  typically 3–9 months. Failures are common on first submission —
  budget for a re-submission.

### 6. Go-live: customer-side PowerShell

Once the bot is certified and deployed, each customer's Teams admin
runs the ``New-CsTeamsComplianceRecordingApplication`` cmdlet sequence
(see ``backend/app/services/teams_recording/policy.py`` for the exact
template). The customer must:

* Have a Microsoft 365 E5 (or A5) tenant — compliance recording is not
  available on lower SKUs.
* Run the cmdlets from a workstation with the MicrosoftTeams PowerShell
  module installed.
* Grant the policy to specific user UPNs:
  ``Grant-CsTeamsComplianceRecordingPolicy -Identity user@example.com
  -PolicyName <our-policy-name>``.

After this, Teams will automatically insert our bot into recorded
users' calls. There is no per-call activation.

## Decision points the user faces

* **Build vs partner.** Some compliance recording vendors (Verint,
  ASC, Theta Lake, Numonix) have already certified bots and license
  them. Reselling their bot might shortcut the certification track.
* **Azure Communication Services Call Recording** is an alternative
  that doesn't require certification but only works for ACS-originated
  calls — it does *not* cover native Teams calls (the entire point of
  this integration). It's not a substitute, but worth knowing about
  if the call mix shifts.
* **Single-tenant vs multi-tenant deployment.** Some enterprise
  customers will refuse to consent to a multi-tenant bot. A
  single-tenant deployment per customer is heavier but unblocks those
  accounts.
* **Recording storage** — Microsoft does not store the bot's media for
  you. We need an Azure Blob Storage strategy with retention,
  encryption-at-rest, and per-tenant isolation.

## Where this scaffold helps when the bot eventually lands

The Python control plane in this repo is the steady-state of the
LINDA-side glue. Notification persistence, the bot-callback contract,
and subscription create/renew/delete/bootstrap are already implemented
and tested (see `DEPLOYMENT_RUNBOOK.md`). When the .NET bot is ready,
the remaining integration work is:

1. Set the Teams env vars on the backend (`DEPLOYMENT_RUNBOOK.md` §2),
   including `TEAMS_BOT_CALLBACK_SECRET` once the bot is about to send
   real traffic.
2. Register a real ``MediaBot`` (via
   ``bot_interface.set_media_bot_factory``) whose ``status().deployed``
   is ``True`` — required to get past `/teams/bot/callback`'s
   "not deployed" 503 gate at all, even just to start receiving
   ``session.started``/``audio.available`` events. It doesn't need a
   fully-implemented ``attach_to_call``/``detach`` (those are the
   *outbound* calls LINDA would make to the bot, which this round
   never exercises) to unlock the *inbound* callback processing.
3. Add the Celery-beat wiring for `renew_due_teams_subscriptions`
   (`DEPLOYMENT_RUNBOOK.md` §3 has the exact task + schedule to add to
   `tasks.py` — not done in this round; `backend/app/tasks.py` is a
   sensitive path this workstream doesn't edit directly).
4. Run `bootstrap_teams_integration` per customer once their Azure AD
   consent + PowerShell (step 6) are done.

None of those changes touch other streams' code or the LINDA
transcription pipeline contract — that's the point of landing the
scaffold now.

## References

* Microsoft compliance recording overview:
  https://learn.microsoft.com/microsoftteams/teams-recording-policy
* Graph Communications Calling SDK:
  https://learn.microsoft.com/graph/cloud-communications-concept-overview
* Sample bots:
  https://github.com/microsoftgraph/microsoft-graph-comms-samples
* Subscription resource docs (callRecords, online meeting recordings):
  https://learn.microsoft.com/graph/api/resources/subscription
* ``New-CsTeamsComplianceRecordingApplication``:
  https://learn.microsoft.com/powershell/module/teams/new-csteamscompliancerecordingapplication
