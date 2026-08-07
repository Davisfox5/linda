# Microsoft Teams Compliance Recording — Deployment Runbook

This is the step-by-step operational companion to `CERTIFICATION_PATH.md`
(the "why" / architecture doc) and `USER_TODO.md` (the flat human
checklist). Use this doc when you're actually executing the rollout —
each step says explicitly whether it's **LINDA-side** (this repo, code
already lands with this round) or **External / Microsoft-controlled**
(you or Microsoft do it outside this repo; nothing here can automate
it).

Read this after `CERTIFICATION_PATH.md`, not instead of it — that doc
has the full architecture rationale and the decision points (build vs.
license the bot, single- vs multi-tenant deployment, storage strategy).
This doc assumes those decisions are already made and walks the actual
sequence.

## What landed in this round (code, already in this repo)

* `POST /teams/notification` persists Graph change notifications:
  `communications/callRecords` entries upsert a `TeamsCallRecord` row;
  `communications/onlineMeetings/getAllRecordings` entries upsert a
  `UcRecordingJob` (`provider="teams_compliance"`) and dispatch the
  existing `fetch_uc_recording` Celery task — the same pipeline
  RingCentral/Webex Calling/Zoom Phone recordings ride.
* `POST /teams/bot/callback` is a versioned (`version: "1"`) contract
  for the (still undeployed) .NET media bot:
  `session.started` / `session.stopped` (persisted as a
  `TeamsCallRecord`) and `audio.available` (bridged straight into an
  `Interaction` + `process_voice_interaction`, the same way
  `POST /interactions/ingest-recording`'s `audio_url` mode works).
* `backend/app/services/teams_recording/teams_graph.py` — Graph
  subscription `create` (already existed) / `renew` / `delete`, plus
  `bootstrap_teams_integration` (creates the per-customer `Integration`
  row + subscriptions together) and `renew_due_teams_subscriptions`
  (the sweep an integrator wires to Celery beat — **not wired by this
  round**, see "Celery-beat wiring" below).
* `backend/app/services/telephony/uc/teams_compliance.py` — the UC
  provider adapter that lets `fetch_uc_recording` pull the actual
  recording bytes from Graph using an app-only bearer, once a
  `UcRecordingJob` exists.

None of this makes a real Teams call get recorded by itself — that
still requires the .NET media bot, which is out of scope for this repo
(see "Media bot hosting" below). What's here is the *receiving* half:
fully tested, ready for real traffic the moment the bot exists.

---

## 1. Azure AD app registration + Graph permissions

**External / Microsoft-controlled** (Azure Portal, your own AAD
tenant — not a customer's).

1. In [portal.azure.com](https://portal.azure.com), **Azure Active
   Directory → App registrations → New registration**.
   - Name: e.g. "LINDA Compliance Recording Bot".
   - Supported account types: **multi-tenant** ("Accounts in any
     organizational directory") — this app will be consented to by
     every customer tenant, not just yours.
2. **Certificates & secrets → New client secret.** Note the secret
   value immediately (shown once). A certificate is preferred for
   production (Microsoft's own compliance-recording samples recommend
   it) but a secret is sufficient to start.
3. **API permissions → Add a permission → Microsoft Graph →
   Application permissions.** Add:
   - `Calls.AccessMedia.All` — required for any media bot; **this is
     the permission Microsoft gates behind compliance-recording
     certification** (see step 5). Requesting it doesn't require
     certification, but *using* it in production does.
   - `Calls.JoinGroupCallAsGuest.All` — non-meeting (ad hoc) calls.
   - `OnlineMeetingArtifact.Read.All` — fetch the recorded artifact
     metadata/content after a meeting completes. This is what
     `teams_compliance.py`'s app-only bearer uses to hit
     `GET /v1.0/communications/onlineMeetings/{id}/recordings/{id}/content`.
4. **Grant admin consent** for your own tenant (the button on the API
   permissions page). This does NOT grant it for customer tenants —
   each customer's admin does that separately in step 6.
5. Note three values from the **Overview** page + the secret from
   step 2:
   - **Application (client) ID** → `TEAMS_BOT_APP_ID`
   - **Directory (tenant) ID** → `TEAMS_TENANT_ID`
   - The secret value → `TEAMS_BOT_APP_SECRET`

## 2. LINDA-side environment variables

**LINDA-side.** Set these as Fly secrets (or `.env` locally) on the
backend deployment. All are read via `backend/app/config.py`
(`Settings`); none have a working default in production — every one
below is empty (`""`) until you set it.

| Var | Source | Purpose |
|---|---|---|
| `TEAMS_BOT_APP_ID` | Step 1.5 | App-only Graph client id |
| `TEAMS_BOT_APP_SECRET` | Step 1.5 | App-only Graph client secret |
| `TEAMS_TENANT_ID` | Step 1.5 | **Your** AAD tenant, not a customer's |
| `TEAMS_GRAPH_CLIENT_STATE` | You generate (`openssl rand -base64 32` or similar) | Shared secret Graph echoes back on every `/teams/notification` delivery (`clientState`); rejects tampered/replayed batches |
| `TEAMS_BOT_CALLBACK_SECRET` | You generate | Shared secret the .NET bot presents as `X-LINDA-Bot-Secret` on `/teams/bot/callback`. **Leave unset until the bot is actually being deployed** — see the note below |
| `PUBLIC_WEBHOOK_BASE_URL` | Already exists (used by Gmail/Graph email push too) | Base URL `teams_graph.notification_url()` builds `.../api/v1/teams/notification` from |

```bash
fly secrets set \
  TEAMS_BOT_APP_ID="..." \
  TEAMS_BOT_APP_SECRET="..." \
  TEAMS_TENANT_ID="..." \
  TEAMS_GRAPH_CLIENT_STATE="$(openssl rand -base64 32)" \
  -a linda-backend-production
```

**About `TEAMS_BOT_CALLBACK_SECRET` staying unset initially:** with it
unset, `POST /teams/bot/callback` runs in a lenient placeholder mode —
it 503s until a real `MediaBot` is registered (it always does today;
see step 4), and once one is, it accepts + logs recognised payloads
without enforcing the secret. This lets you validate TLS/ingress/IP
allowlisting for the callback URL against a not-yet-secret-bearing
health check before the bot ships. Set the secret (and give it to
whoever builds/licenses the bot) once you're pointing real bot traffic
at the endpoint — from then on, a missing/wrong secret is a hard 401.

## 3. Subscription bootstrap (per customer tenant)

**LINDA-side**, run once per customer after they've completed step 6
below (Azure AD consent). There is no admin UI for this yet — it's a
plain async function, intentionally not wired to an HTTP route this
round (see `teams_graph.bootstrap_teams_integration`'s docstring).
Run it from a one-off shell against the production DB session, e.g.:

```python
import asyncio
from backend.app.db import async_session
from backend.app.services.teams_recording.teams_graph import bootstrap_teams_integration

async def main():
    async with async_session() as db:
        integ = await bootstrap_teams_integration(
            db,
            tenant_id=<linda_tenant_uuid>,
            aad_tenant_id="<customer's Azure AD tenant id>",
        )
        print(integ.id, integ.provider_config)

asyncio.run(main())
```

This creates (or refreshes) the `teams_compliance` `Integration` row
for that tenant and registers a Graph subscription for both
`communications/callRecords` and
`communications/onlineMeetings/getAllRecordings`, storing the
subscription ids + expirations in
`Integration.provider_config["graph_subscriptions"]`. Requires
`TEAMS_GRAPH_CLIENT_STATE` and `PUBLIC_WEBHOOK_BASE_URL` to be set
(raises `SubscriptionValidationError` otherwise).

**Where the customer's `aad_tenant_id` comes from:** their Teams admin
can read it from the Azure Portal (Azure AD → Overview → Tenant ID),
or you can capture it from the admin-consent redirect in step 6
(Microsoft appends `tenant=<guid>` to the redirect URL).

### Celery-beat wiring the integrator must add (not done by this round)

`teams_graph.py` deliberately never imports or edits
`backend/app/tasks.py` or `main.py` (the sensitive-path rule for this
workstream). To make subscription renewal actually run on a schedule,
add — in `backend/app/tasks.py`:

```python
@celery_app.task(name="renew_teams_subscriptions")
def renew_teams_subscriptions() -> Dict[str, Any]:
    import asyncio
    from backend.app.db import async_session
    from backend.app.services.teams_recording.teams_graph import (
        renew_due_teams_subscriptions,
    )

    async def _run():
        async with async_session() as db:
            return await renew_due_teams_subscriptions(db)

    return asyncio.run(_run())
```

and a Celery-beat schedule entry (Graph's shortest-lived subscribed
resource — `onlineMeetings/getAllRecordings` — caps at ~60 minutes, so
beat must run more often than the `within_minutes` slack passed to
`renew_due_teams_subscriptions`, default 15):

```python
"renew-teams-subscriptions": {
    "task": "renew_teams_subscriptions",
    "schedule": crontab(minute="*/15"),
},
```

Follow whichever async-session-from-sync-Celery-task idiom
`backend/app/tasks.py` already uses elsewhere in that file (e.g. how
`fetch_task.py`/`tasks.py` bridge `asyncio.run` into a sync Celery
task) — that idiom lives in a file this workstream doesn't touch, so
match its existing style rather than inventing a second one.

**Without this wiring, subscriptions expire and Graph silently stops
sending notifications** (Graph does not retry or alert you — it just
stops). Do not skip this before going live with a real customer.

## 4. Media bot hosting requirements

**External — a separate, out-of-scope engineering workstream.** This
is the single biggest piece of work in the whole integration and is
explicitly NOT built by this repo. See `CERTIFICATION_PATH.md` §"The
certification track" step 4 for the full detail; summary:

* **Must be .NET (C#).** Microsoft's Graph Communications Calling SDK
  has no Python/Go/Node binding.
* **Hosting**: Windows VM, Azure App Service for Containers, Azure
  Container Apps, or AKS — Microsoft's own samples default to App
  Service; ACA/AKS if you need per-tenant isolation at scale.
* **Certified media SDK**: fork/scaffold from
  [microsoft-graph-comms-samples](https://github.com/microsoftgraph/microsoft-graph-comms-samples)
  (`ComplianceRecordingBot` sample is the closest starting point).
* **Public HTTPS endpoint** with a certificate from a public CA
  (self-signed fails certification).
* **Must call back into LINDA** at `POST /api/v1/teams/bot/callback`
  with the `version: "1"` envelope this round already receives:
  `session.started` (call/organizer/join_url), `session.stopped`
  (reason), `audio.available` (an HTTPS `audio_url` — wherever the bot
  staged the recorded audio, e.g. Azure Blob Storage — plus
  `duration_seconds`). See
  `backend/app/services/teams_recording/bot_callback.py` for the exact
  schema and `tests/test_teams_bot_callback.py` for worked examples.
* Every payload must include `aad_tenant_id` — the customer's Azure AD
  tenant id — so LINDA can route it to the right `teams_compliance`
  `Integration` row (see step 3). The bot doesn't need to know LINDA's
  internal tenant ids at all.
* Every payload must include the `X-LINDA-Bot-Secret` header matching
  `TEAMS_BOT_CALLBACK_SECRET` once that's provisioned (step 2).

Estimated effort per `CERTIFICATION_PATH.md`: 3–6 engineer-months for
a first cut, plus certification iteration (step 5).

## 5. Microsoft 365 certification steps and timelines

**External / Microsoft-controlled.** In order:

1. **Microsoft Partner Center registration** — sign up at
   [partner.microsoft.com](https://partner.microsoft.com/), complete
   publisher verification (D-U-N-S number, signed attestation).
   *Lead time: 4–8 weeks* if not already enrolled. Do this FIRST — it
   gates everything else.
2. Build and internally test the media bot (step 4) against a
   sandbox/dev Teams tenant — no certification needed for this.
3. **Open a Partner Center support case** under the "Teams compliance
   recording" certification track.
4. **Submit the certification kit**: end-to-end demo video of the bot
   recording a real call, an architecture diagram with explicit
   data-flow/data-residency claims, a security review (pen-test
   results, secret-rotation process), and a privacy posture (DPA
   template, retention defaults).
5. Microsoft assigns a certification engineer. *Typical timeline:
   3–9 months*, with back-and-forth review rounds. First-submission
   rejections are common — budget for at least one resubmission cycle.
6. On approval, Microsoft lifts the production restriction on
   `Calls.AccessMedia.All` for your app registration.

**Nothing in this repo can shortcut this.** `Calls.AccessMedia.All` in
this app registration works in a dev/test Teams tenant before
certification (Microsoft allows self-testing), but not in production
against real customer tenants until certified.

## 6. Customer-side compliance-recording policy assignment (PowerShell)

**External — the customer's Teams admin runs this**, per customer,
after your bot is certified and deployed (or, for pilot/sandbox
customers who've explicitly agreed to a pre-certification trial, once
you've told them the bot is functional for testing).

1. Confirm the customer has **Microsoft 365 E5 or A5** — compliance
   recording is not available on lower SKUs.
2. Generate their PowerShell script via
   `backend.app.services.teams_recording.policy.render_powershell`
   (already implemented, tested in `tests/test_teams_subscriptions.py`):

   ```python
   from backend.app.services.teams_recording.policy import (
       CompliancePolicyTemplate, render_powershell,
   )
   script = render_powershell(CompliancePolicyTemplate(bot_app_id="<TEAMS_BOT_APP_ID>"))
   ```

3. Send `script` to the customer's Teams admin. They run it on a
   workstation with the `MicrosoftTeams` PowerShell module installed
   (`Install-Module MicrosoftTeams`) and a Teams-admin-privileged
   `Connect-MicrosoftTeams` session. It:
   1. Registers your bot as a compliance-recording application
      (`New-CsTeamsComplianceRecordingApplication`).
   2. Creates (or updates) the `LINDA-CompliancePolicy` policy.
   3. Attaches the bot to the policy.
4. The admin then grants the policy to specific users:
   ```powershell
   Grant-CsTeamsComplianceRecordingPolicy -Identity user@example.com -PolicyName LINDA-CompliancePolicy
   ```
   There is no per-call activation — once granted, Teams automatically
   inserts your bot into that user's calls going forward.
5. Get the customer's Azure AD tenant id and run step 3
   (`bootstrap_teams_integration`) if you haven't already for this
   customer.
6. Give the customer's networking team `PUBLIC_WEBHOOK_BASE_URL` +
   `/api/v1/teams/notification` for their egress allowlist, if they
   enforce one.

## 7. Verification checklist

Run through this after steps 1–3 (before the bot exists, to validate
infra) and again after step 4/6 (with a real bot + real customer
policy):

**Infra-only (bot not deployed yet):**

- [ ] `GET https://<your-domain>/api/v1/teams/notification` is
      reachable from the public internet (test with a plain `curl`;
      Graph itself will send the real validation handshake).
- [ ] Manually POST a validation handshake and confirm a 200 with the
      token echoed back as `text/plain`:
      `curl -X POST "https://.../api/v1/teams/notification?validationToken=test123"`
      → body is exactly `test123`.
- [ ] `POST /api/v1/teams/bot/callback` (no body) returns 503 with
      `{"deployed": false, ...}` — confirms the stub `MediaBot` is
      still the honest default and nothing claims readiness
      prematurely.
- [ ] `bootstrap_teams_integration` succeeds against a real (or Graph
      Playground sandbox) tenant and the resulting
      `Integration.provider_config["graph_subscriptions"]` has two
      entries with future `expiration` values.
- [ ] Confirm the Celery-beat renewal task (step 3) is scheduled and
      its next run is within `within_minutes` of the soonest
      subscription expiry (`onlineMeetings/getAllRecordings` — 60 min
      cap).

**End-to-end (bot deployed, certified or pilot-consented):**

- [ ] A test call in the pilot customer's tenant triggers a
      `communications/callRecords` notification → a `TeamsCallRecord`
      row appears with `certification_status="bot_required"` if the
      bot didn't attach, or reflects the bot's own callback state if
      it did.
- [ ] The bot's `session.started` callback (with the correct
      `X-LINDA-Bot-Secret`) produces a `TeamsCallRecord` row.
- [ ] The bot's `audio.available` callback produces an `Interaction`
      row with `audio_url` set, `source="teams_compliance"`, and
      `process_voice_interaction` dispatched (check Celery task logs /
      the interaction's `status` transitioning off `"processing"`).
- [ ] The transcript for that interaction appears in the normal LINDA
      UI, indistinguishable from a RingCentral/Webex/Zoom Phone
      recording's transcript.
- [ ] `communications/onlineMeetings/getAllRecordings` notifications
      (Teams' own built-in meeting recording, independent of the
      compliance bot) upsert a `UcRecordingJob` and the
      `fetch_uc_recording` Celery task completes with `state="done"`.

---

## Summary: who does what

| Step | Owner |
|---|---|
| 1. Azure AD app registration + Graph permissions | External (you, in Azure Portal) |
| 2. LINDA env vars | LINDA-side (this repo's deploy config) |
| 3. Subscription bootstrap + Celery-beat wiring | LINDA-side (code exists; wiring + running it is an operator action) |
| 4. Media bot build + hosting | External (separate .NET workstream, out of scope for this repo) |
| 5. Microsoft certification | External (Microsoft-controlled, 3–9+ months) |
| 6. Customer PowerShell + policy grant | External (each customer's Teams admin) |
| 7. Verification | Mixed — infra checklist is LINDA-side; end-to-end checklist needs the external bot |

See also: `CERTIFICATION_PATH.md` (architecture + decision points),
`USER_TODO.md` (flat checklist), and
`backend/app/services/teams_recording/` (the code this doc describes).
