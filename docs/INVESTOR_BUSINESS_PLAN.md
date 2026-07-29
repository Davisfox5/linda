# LINDA — Investor & Customer Business Plan

*Consolidated reference document. Synthesizes [BUSINESS_PLAN.md](BUSINESS_PLAN.md),
[PRICING_MODELS.md](PRICING_MODELS.md) (v2), [ARCHITECTURE.md](../ARCHITECTURE.md), and the
public marketing site. Use this as the master source when building investor decks,
customer one-pagers, and partner enablement materials.*

**Product:** LINDA — *Listening Intelligence and Natural Dialogue Assistant*
(formerly CallSight) · **Domain:** lindaai.net · **Entity:** © 2026 LINDA

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [The Opportunity](#2-the-opportunity)
3. [Product & Technology](#3-product--technology)
4. [Business Model & Pricing](#4-business-model--pricing)
5. [Competitive Landscape](#5-competitive-landscape)
6. [Go-to-Market Strategy](#6-go-to-market-strategy)
7. [Target Customers (Named)](#7-target-customers-named)
8. [White-Label / Channel Strategy (Named Partners)](#8-white-label--channel-strategy-named-partners)
9. [Traction & Product Readiness](#9-traction--product-readiness)
10. [Financial Model](#10-financial-model)
11. [Target Investors (Named) & The Ask](#11-target-investors-named--the-ask)
12. [Team](#12-team)
13. [Risks & Mitigation](#13-risks--mitigation)
14. [Appendix: Pricing & KPI Reference](#14-appendix-pricing--kpi-reference)

---

## 1. Executive Summary

**LINDA is the only turnkey conversation-intelligence platform built to be resold under
someone else's brand.** It automatically transcribes every customer call, then uses AI
(Anthropic Claude) to produce the things that actually change revenue: assigned action
items, follow-up drafts, real-time rep coaching, QA scorecards, and churn/upsell signals.

Two things make it a business, not a feature:

1. **It closes the loop after the call.** Most tools stop at the transcript. LINDA turns
   conversations into tracked, assigned follow-ups that flow into the CRM the team already
   uses. The value is in what happens *after* the call.
2. **It is multi-tenant and white-label from the ground up.** The same platform can be
   deployed as LINDA's own product to a sales team of any size — from a 5-seat agency to a
   500-rep org — *or* rebranded end-to-end (custom domain, logo, "invisible LINDA") and
   resold by a telecom, UCaaS provider, or BPO as their own AI feature.

**The wedge:** the conversation-intelligence market is projected to exceed **$40B by 2030**,
but it is barbell-shaped. Enterprise incumbents (NICE, Verint, CallMiner) cost $150K–$2M+
and take months to deploy. AI-native startups (Gong, Observe.AI, Cresta) sell only their
own brand. Pure APIs (Deepgram, Symbl.ai) make you build the product yourself. **No one
offers a complete, rebrandable product to the underserved mid-market and to the thousands
of telecom/CRM/BPO partners who want to sell AI call intelligence but will never build it.**
LINDA occupies that gap.

**Two revenue engines — channel leads, direct floors:**
- **White-label / channel (lead engine, opening now)** — white-label wholesale providers,
  CPaaS, UCaaS, master agents/TSDs, vertical SaaS/CRM vendors, and BPOs reselling under
  their brand, $25K–$500K+ ARR each. *Expected to become the majority of revenue; the
  partner program is build-complete and this is the immediate motion.*
- **Direct (SMB now, mid-market later)** — self-serve/light-touch SMB teams (typically
  under ~50 employees, 5–25 seats) provide an independent revenue floor and product proof;
  mid-market sales & support teams (10–500 reps, $12K–$300K ARR each) open as the
  expansion motion once channel and SMB reference logos exist. The platform remains
  individually purchasable at any size throughout.

**The financial shape** (modeled, illustrative): **$500K → $3M → $12M** revenue across the
first three years at **75–80% gross margin**, with net income turning positive by Year 2.

**The ask:** a seed / seed-extension round to stand up the first cohort of white-label
distributors and fund the SMB self-serve beachhead, with mid-market direct sales layered
on once reference logos exist. See [§11](#11-target-investors-named--the-ask).

---

## 2. The Opportunity

### 2.1 The problem

| Pain point | Who feels it | Today's workaround |
|---|---|---|
| Reps forget action items from calls | Sales & support teams | Manual notes, post-call CRM entry |
| Managers can't review every call | Sales / support leaders | Random sampling, anecdotal feedback |
| No structured follow-up process | Revenue teams | Inconsistent follow-up, lost deals |
| Customers repeat themselves across calls | Support orgs | Agents re-read ticket history by hand |
| Telephony/CRM platforms have no built-in intelligence | UCaaS / CRM / BPO vendors | Buy or build separate tooling; poor UX |

**Core insight:** the value isn't the transcript — it's what happens after the call. LINDA
turns conversations into assigned, tracked outcomes.

### 2.2 Market sizing

| Layer | Definition | Estimate |
|---|---|---|
| **TAM** | Global conversation-intelligence / contact-center AI | **>$40B by 2030** |
| **SAM** | The white-label channel (wholesale providers, TSDs, CPaaS/UCaaS, vertical SaaS, BPOs) + the SMB and mid-market sales & support orgs (5–500 reps) it serves | Multi-billion; the segments priced out of NICE/Verint but underserved by branded-only AI-native tools — and, at the small end, ignored by Gong entirely |
| **SOM (3-yr target)** | ~400 direct customers + 10 live white-label distributors | **~$12M ARR** (see [§10](#10-financial-model)) |

**Why now:**
- Generative AI made deep call analysis cheap enough to sell at mid-market price points.
- Incumbents are retrofitting gen-AI onto 2000s-era architectures — a real but closing window.
- Smaller CCaaS/UCaaS vendors need **AI parity** with Genesys/Talkdesk and cannot build it.
- Regulatory tailwinds (MiFID II, Dodd-Frank, HIPAA, state recording laws) push call
  capture + analysis from "nice to have" to "mandated."

---

## 3. Product & Technology

### 3.1 What LINDA does

LINDA ingests customer-conversation audio and text — phone, VoIP, video conferencing,
uploaded recordings, transcripts, and email — transcribes it, and runs AI analysis to
produce next steps, follow-ups, sentiment/churn/upsell signals, QA scorecards, and live
coaching. It is sold to **sales, customer success, and support** teams.

**Fourteen shipped capabilities** (from the marketing site and codebase):

| # | Capability | What it does |
|---|---|---|
| 1 | Omnichannel intelligence | Audio + transcripts + email in one place |
| 2 | Real-time transcription | Deepgram Nova-3 + self-hosted Whisper large-v3 fallback; diarization |
| 3 | AI insights | Claude-generated summaries, next steps, follow-up email drafts, coaching |
| 4 | Sentiment analysis | Per-call and trend-level emotion/sentiment |
| 5 | Auto action items | Extracted, assigned, and tracked to completion |
| 6 | Live agent coaching | Real-time hints, KB search, compliance prompts, manager whisper |
| 7 | Scorecards & QA | 100% of interactions scored against custom rubrics |
| 8 | Call library | Auto-curated exemplary / improvement moments |
| 9 | Call metrics | Talk/listen ratio, filler words, speech rate, interruptions (zero AI cost) |
| 10 | Full-text search | Elasticsearch across all transcripts |
| 11 | Predictive analytics | Churn risk, upsell signals, escalation & agent-burnout prediction |
| 12 | Contact intelligence | Phone matching, mid-call name recognition, 2-way CRM sync |
| 13 | White-label | Custom domain + full rebrand — "your clients never know it's us" |
| 14 | API-first | Headless mode for partners who integrate, not just dashboard users |

**The differentiated workflow product — "Action Plans":** LINDA synthesizes multi-step
follow-up plans from a call, drafts the emails, tracks commitments, matches inbound email
replies (RFC 822 threading), grounds plans in the customer's own KB procedures via vector
search, and syncs CRM deal context. This is the "after the call" loop that competitors leave open.

### 3.2 Architecture (why it's defensible to operate)

- **Backend:** FastAPI (~56 routers), ~89-table data model centered on `Interaction`;
  Celery/Redis async pipeline with exactly-once correctness guarantees.
- **Front end:** Next.js 15 / React 19 SPA; static marketing + interactive demo site.
- **AI:** Anthropic Claude, tiered **Haiku / Sonnet / Opus** — each touchpoint on the
  cheapest model that meets its quality bar; prompt caching is load-bearing for margin.
  Every model id resolves through a single catalog, so a version bump or provider failover
  is a one-line change.
- **Transcription:** Deepgram (speed) + self-hosted Whisper (cost) — **pluggable ASR**, no
  single-vendor lock, letting us tune cost/quality per tier and per customer.
- **Telephony-agnostic ingestion:** SIPREC (Cisco CUBE, Avaya SBCE, Metaswitch), UC vendor
  APIs (RingCentral, Webex Calling, Zoom Phone), Genesys AudioHook (live on AppFoundry),
  CPaaS (Twilio, SignalWire, Telnyx). Microsoft Teams compliance recording is scaffolded
  behind MS certification.
- **Multi-tenancy:** Postgres row-level security across all tenant-scoped tables; one Qdrant
  choke point with mandatory tenant filtering; per-tenant custom domains and API keys. This
  is what makes true white-label safe.
- **Security posture:** SOC 2 Type II controls, TLS 1.3, encryption at rest (S3 SSE-AES256),
  Fernet-encrypted OAuth tokens, PII redaction (Presidio), HIPAA BAA / GDPR DPA available,
  published `security.txt`. Google CASA assessment and OAuth verification in progress for
  Gmail/Calendar/Contacts scopes (email ingestion, scheduling, contact enrichment).

---

## 4. Business Model & Pricing

Two revenue engines share one platform. Pricing below reflects the current **v2** model
(supersedes the older $49/$89 figures in the legacy business plan).

### 4.1 Direct pricing (per seat / month, list)

| Tier | 1–10 seats | 11–25 seats | 25+ seats | Positioning |
|---|---|---|---|---|
| **Starter** | $39 | $34 | $29 | Transcription, search, AI summaries — "get the basics right" |
| **Professional** | $89 | $79 | $69 | + AI deep analysis, PII redaction, KB Q&A, CRM sync — "coach smarter, sell more" |
| **Business** | $179 | $159 | $139 | + live coaching, 100% auto-QA, custom scorecards — "run a revenue machine" |
| **Enterprise** | Custom (from $299) | — | — | Dedicated tenant, SSO/SCIM, BYO-AI-key, on-prem option, SLA |

- **Annual commit:** 20% off; 25% off on 2-year.
- **Usage cap:** 5,000 audio-min/seat/mo; overage $0.02/min (rarely hit at realized usage).
- **Free trial:** 14-day sandbox tenant (same codebase, feature-flag-gated).
- **Add-on SKUs:** individual higher-tier features (Live Coaching $29, Auto-QA $25, SSO
  $99/mo flat, Custom Domain $49/mo, etc.) — rational for one, steer to upgrade at two+.

### 4.2 White-label / channel pricing

Three partner tiers set wholesale discount off list:

| Partner tier | Qualification | Wholesale discount | Support model |
|---|---|---|---|
| **Authorized Reseller** | 50–250 seats, $25K+ ARR | 35% off list | LINDA handles Tier 2/3 |
| **Premier Partner** | 250–1,000 seats, $150K+ ARR | 45% off list | LINDA handles Tier 3 |
| **Elite Distributor** | 1,000+ seats, $500K+ ARR | 55–60% off list | Partner Tier 1–2, LINDA Tier 3 |

Plus deal registration (partner-first conflict policy, +5–7% bonus), MDF (2–4%), and three
packaging options (co-branded "Powered by LINDA" → standard white-label → full white-label +
custom domain). **Worked example:** one Elite distributor with 10 × 50-seat Business
deployments drives **~$381K ARR to LINDA** at minimal direct-sales cost, while clearing 60%
margin for the partner.

### 4.3 Unit economics (target at scale)

| Metric | Direct | Partner channel |
|---|---|---|
| Gross margin | 75–80% | 75–80% (wholesale) |
| CAC | $800–$1,200 | $200–$400 |
| LTV (per seat) | ~$2,400 (48-mo life) | Higher (lower churn, larger deployments) |
| LTV:CAC | ~3:1 | ~6:1+ |
| Payback | <12 months | Faster |
| Churn target | <3% logo / <1% net revenue | Lower |

**Margin mechanics:** transcription dominates COGS (~$38.50/seat at the 5K cap on Deepgram
streaming), so lower tiers route to Whisper + Haiku and realized usage averages ~2,000
min/seat — that's what makes 75–80% blended margin achievable at these price points.

---

## 5. Competitive Landscape

```
                    FULL PLATFORM (turnkey)
                            │
     NICE · Verint          │        Gong · Chorus
     CallMiner · Calabrio   │        Observe.AI · Cresta · Balto · Uniphore
                            │
 LEGACY / INCUMBENT ────────┼──────────── AI-NATIVE / MODERN
                            │
     Genesys · Talkdesk     │     ★ LINDA ★
     AWS Contact Lens       │        Symbl.ai
     Google CCAI · Nuance   │
                            │
                    API / EMBEDDABLE LAYER
                            │
                   Deepgram · AssemblyAI · Rev.ai · Speechmatics
```

**The gap LINDA fills:** every quadrant leaves a hole.
- **Enterprise incumbents** (NICE, Verint, CallMiner): $150K–$2M+, 3–6 month deploys, cannot
  be white-labeled.
- **AI-native** (Gong, Observe.AI, Cresta, Balto, Uniphore): modern, but branded-only — no
  partner/embed channel, and mostly single-use-case (support *or* sales).
- **Pure APIs** (Deepgram, AssemblyAI, Symbl.ai): building blocks — the customer builds the
  product, UI, and workflows themselves.
- **Hyperscalers** (AWS Contact Lens, Google CCAI, Microsoft/Nuance): lock you into their
  cloud/telephony stack.

**LINDA is the only position combining a complete turnkey product (dashboard + workflows +
integrations) with true white-label multi-tenancy and API-first embeddability.** Key
differentiators no competitor matches together: rebrandable + custom domain + multi-tenant +
follow-up email drafts + telephony-agnostic + days-to-deploy + serves *both* sales and support.

**Most direct competitor:** Symbl.ai (embedded/OEM API) — but they sell blocks; we sell a
finished, rebrandable product. **Biggest long-term threat:** hyperscaler bundling and
Deepgram/AssemblyAI moving up-stack — mitigated by our workflow depth, white-label channel
lock-in, and multi-model optionality. (Full 13-row differentiation matrix and per-competitor
deep dives in [BUSINESS_PLAN.md §4](BUSINESS_PLAN.md).)

---

## 6. Go-to-Market Strategy

**One funnel, three motions, gated on milestones (not calendar).** These reconcile the free
tier, the consultative "45-Day Preview" on the marketing site, and the channel program.
**Sequencing: channel and SMB direct open together in Phase 1; mid-market direct is the
Phase 3 expansion**, sold against the reference logos and reference pricing the first two
motions establish. The platform remains individually purchasable at any size in every phase.

### Phase 1 — Channel Ignition + SMB Beachhead
**Gate to exit:** 2 live white-label partners (incl. one wholesale-provider flagship);
~50 paying SMB customers; 5–10 reference case studies.
- **Channel track (lead):** the partner program is build-complete (see [§8.3](#83-partner-enablement-ready-now)) —
  open it now. Sign the flagship white-label wholesale provider (Reinvent Telecom profile)
  and one vertical-SaaS embed (the live Flex integration is the template). Start one
  TSD/master-agent conversation. Presence at Channel Partners Conf & Expo.
- **SMB direct track:** free 14-day sandbox + PLG content (SEO on "call transcription
  software," "AI sales coaching," and SMB-vertical compliance terms like "medicare call
  recording"), Product Hunt, founder-led LinkedIn, dev marketing (API docs, SDKs).
  Self-serve Sandbox/Starter close; owner/GM buyers; time-to-value in hours.
- **Close motion for larger/stretch deals:** the consultative **45-Day Preview** —
  Discovery → Custom Build → Proof of Concept on *their own* call data → Integration & live
  trial → "You Decide" pricing proposal, no lock-in.
- **Proof asset:** run the POC on the prospect's real calls — the demo *is* their data.

### Phase 2 — Channel Scale + SMB Verticalization
**Gate to exit:** ~$500K ARR (majority channel + SMB); 10 partners in pipeline.
- Hire 1 Partner Manager first; next GTM hire follows whichever motion is winning.
- Scale the partner program (deal reg, portal, certification, enablement kit); co-selling
  with early partners.
- Verticalize the SMB playbooks (insurance agencies, legal intake, home services, fitness).
- Conference presence where the channel is: Channel Partners Conf & Expo first, then
  Customer Contact Week, Twilio SIGNAL.

### Phase 3 — Mid-Market Expansion + Channel Acceleration
**Gate to exit:** ~$3M ARR; 5+ live white-label distributors; 50 in pipeline; first
mid-market direct logos.
- **Mid-market direct opens here:** hire mid-market AEs and run the 45-Day Preview against
  the ICP in [§7](#7-target-customers-named), sold with channel/SMB proof in hand.
- Scale the partner program with dedicated enablement; co-marketing + MDF.
- Marketplace listings: Salesforce AppExchange, HubSpot Marketplace, Twilio Marketplace,
  Genesys AppFoundry, RingCentral App Gallery.
- Verticalize (healthcare, financial services, real estate) and expand internationally
  (UK/DACH/ANZ — Whisper covers 90+ languages, an edge over English-first competitors).

**Marketing mix:** Content/SEO 25% · Paid search 20% · LinkedIn 15% · Events 15% · Partner
co-marketing 15% (lowest CAC) · DevRel 10%.

**Why white-label is the primary engine:** it inverts the CAC problem. One signed Elite
distributor replaces dozens of direct sales cycles, comes with a built-in customer base, and
churns less. Direct sales exists to prove the product, generate case studies, and set the
reference price the channel sells against.

---

## 7. Target Customers (Named)

*Sequencing note: the immediate direct motion is **SMB** (typically under ~50 employees,
5–25 seats — independent insurance agencies with CMS Medicare recording mandates, legal
intake, home services, medical/dental, fitness studios per the live Flex motion — with
stretch to ~200 employees when a compliance driver or warm channel intro makes the
circumstances right). The vertical table below is the **mid-market expansion ICP for
Phase 3**, retained because the channel partners signed in Phase 1–2 sell into exactly
these accounts — mid-market orgs (50–500 employees, $10M–$500M revenue) with sizable
inside-sales or contact-center operations, already on Salesforce/HubSpot + Zoom/Teams +
Twilio/RingCentral. Buyers: VP Sales, CRO, VP Customer Success, Head of Support. Presented
as target profiles, not committed pipeline.*

| Vertical | Why they fit | Representative target accounts |
|---|---|---|
| **B2B SaaS (inside sales)** | High-volume SDR/AE calls; coaching-hungry; CRM-native | Mid-market SaaS with 50–300 reps — e.g. companies like ZoomInfo-scale-down peers, vertical SaaS vendors, martech/sales-tech mid-caps |
| **Financial services / fintech** | Recording mandates (Dodd-Frank/MiFID), QA pressure | Regional broker-dealers, mid-market lenders, wealth-advisory networks, fintech lenders with call-based sales |
| **Insurance** | Agent networks, compliance scripting, high call volume | Regional carriers, MGAs, insurtech agencies, benefits brokers |
| **Healthcare / health-tech** | HIPAA-bound patient/member call centers | Provider groups, RCM/billing companies, telehealth & patient-access orgs |
| **Real estate / proptech** | Large agent/ISA phone teams, follow-up is the product | Brokerage networks, mortgage originators, proptech lead-gen firms |
| **Professional & home services** | Franchise/agency call centers, lead conversion | Multi-location services businesses, franchisors, agencies |

**How to use this list:** pick 20–30 named accounts per vertical that match the ICP filters
(headcount, tech stack, call volume), and run them through the 45-Day Preview. The strongest
early logos are those with (a) a measurable revenue metric tied to call outcomes and (b) a
compliance or QA driver that makes the buy non-discretionary.

**Anti-targets:** Fortune 500 contact centers (owned by NICE/Verint; sales cycle too long for
our stage) and solo/prosumer *individual* users (served by Fireflies/Otter; wrong unit
economics). Note the distinction: SMB *teams* (5–25 seats with a compliance or
missed-revenue driver) are now a primary target; a single rep wanting personal note-taking
is not.

---

## 8. White-Label / Channel Strategy (Named Partners)

**This is the primary revenue engine, and it opens in Phase 1 — not after a direct-sales
proving period.** The distribution already exists — the U.S. UCaaS/telecom market sells
through **master agents / Technology Solutions Distributors (TSDs)** and thousands of MSPs.
LINDA plugs in as the "AI call intelligence" line item in catalogs that already have a
salesforce and a customer base. The white-label wholesale providers (Reinvent
Telecom/Saddleback profile) are the tip of the spear: their entire business is packaging
rebrandable services for downstream resellers, so one signing activates a whole reseller
network.

### 8.1 Named target channel partners

| Channel type | Why they need LINDA | Named targets |
|---|---|---|
| **Flagship Elite Distributor** | Already modeled in pricing; UCaaS reseller wanting an AI upsell | **Reinvent Telecom** (flagship) |
| **Master agents / TSDs** | Own the reseller relationships for all of UCaaS; one signing unlocks hundreds of sub-agents | **Telarus, AVANT Communications, Intelisys (ScanSource), Sandler Partners, Bridgepointe** |
| **CPaaS / programmable telecom** | Have the media stream, lack the intelligence layer; API-first fit | **SignalWire, Telnyx, Bandwidth, Plivo, Sinch** |
| **UCaaS / CCaaS needing AI parity** | Compete with Genesys/Talkdesk AI but can't build it | **Nextiva, Ooma, GoTo, Vonage, Dialpad-tier challengers, regional carriers (Windstream Enterprise, TPx)** |
| **BPOs / outsourced contact centers** | Want to offer analytics/QA to their own clients as a branded product | Regional/mid-tier BPOs (below the TTEC/Concentrix/Alorica giants), plus specialist verticals (healthcare RCM, collections, insurance) |
| **Vertical CRM vendors** | Want native call intelligence without an R&D team | Industry-specific CRMs (real estate, insurance, home services, healthcare) |

### 8.2 Why this channel converts

- **AI parity is existential** for smaller UCaaS/CCaaS players — Genesys and Talkdesk ship AI
  natively; challengers must answer or lose deals. LINDA is a buy-vs-3-years-of-build answer.
- **Master agents are a force multiplier:** signing one TSD exposes LINDA to their entire
  sub-agent network. This is how the market already buys UCaaS — LINDA rides the same rails.
- **The economics work for the partner:** at Elite discount, a partner clears ~60% margin
  while *undercutting* MiaRec, Dubber, and Xima on end-customer price.
- **Genesys AppFoundry and RingCentral App Gallery** are live/near-live distribution surfaces
  — inbound channel discovery, not just outbound partner sales.

### 8.3 Partner enablement (ready now)

Partner portal (deal reg, reporting, billing), 4-hour engineer certification, sales
enablement kit (decks, battlecards, ROI tool), co-selling assist (Premier+), roadmap input
(Elite). The program structure is built; **the decision has been made to open it in
Phase 1** — channel is the lead motion, not a later-phase upside.

---

## 9. Traction & Product Readiness

*What's real today vs. what's gated on first customers — investors will ask; be precise.*

**Built and running:**
- Full platform shipped: 56-router backend, multi-tenant with row-level security across all
  tenant-scoped tables, async pipeline with exactly-once correctness, SPA, marketing + demo site.
- 14 product capabilities live (see [§3.1](#31-what-linda-does)); "Action Plan" follow-up
  workflow wired end-to-end.
- Telephony integrations landed: SIPREC (Cisco/Avaya/Metaswitch), RingCentral, Webex Calling,
  Zoom Phone, Genesys AudioHook.
- Security posture: SOC 2 Type II controls, encryption, PII redaction, tenant isolation
  hardened (RLS on all tenant tables; Qdrant single-choke-point with mandatory tenant filter).
- Pricing, packaging, and the full white-label channel program are defined and codified
  (`backend/app/plans.py`, Stripe price IDs).

**Gated on first real customers / final steps:**
- Live CRM refresh (HubSpot/Salesforce), production email send/receive, and Voyage embeddings
  for KB-grounded Action Plans await real client accounts.
- Microsoft Teams compliance recording is scaffolded, behind MS certification.
- Google CASA assessment + OAuth verification in progress for Gmail/Calendar/Contacts scopes.

**Honest framing for investors:** the technical risk is largely retired — this is a built,
multi-tenant platform, not a prototype. The open risk is **commercial**: signing the first
white-label distributors and proving the SMB self-serve beachhead. That is precisely what
the raise funds.

---

## 10. Financial Model

*Modeled, illustrative three-year projection (from the base plan). Directional, not a forecast.*

| | Year 1 | Year 2 | Year 3 |
|---|---|---|---|
| **Revenue** | $500K | $3M | $12M |
| — Direct | $350K | $1.8M | $6M |
| — White-label / channel | $150K | $1.2M | $6M |
| **COGS** (infra + AI) | $125K | $660K | $2.4M |
| **Gross profit** | $375K | $2.34M | $9.6M |
| **Gross margin** | 75% | 78% | 80% |
| **Operating expenses** | $960K | $2.1M | $5.5M |
| **Net income (loss)** | ($585K) | $240K | $4.1M |
| **Headcount** | 8 | 18 | 35 |
| **Live white-label partners** | 2 | 10 | — |
| **Paying customers** | 100 | 400 | — |

**Key assumptions:** avg direct deal ~$3,500/mo and white-label ~$8,000/mo by Year 2; AI API
costs decline 15–20%/yr as model efficiency improves; 75% of revenue on annual contracts by
Year 2; net revenue retention >110% (Y1) → >120% (Y2). **The inflection is the channel:**
direct scales linearly with headcount; white-label scales with partner count, and each Elite
partner is a ~$380K ARR unit.

---

## 11. Target Investors (Named) & The Ask

**Stage/shape:** applied-AI vertical SaaS with a white-label distribution wedge and a real,
shipped product. This profile fits (a) funds with a proven conversation-intelligence thesis,
(b) applied-AI / B2B-SaaS seed specialists, and (c) strategic/channel investors whose
platforms LINDA extends. Lead with the third group — a strategic check also validates the
channel thesis.

### 11.1 Thesis-fit — conversation intelligence & GTM SaaS
These funds have already made money in this exact category and understand the buyer.

| Investor | Why they fit | Angle to lead with |
|---|---|---|
| **Emergence Capital** | Led Gong; deepest CI/sales-tech thesis in venture | The mid-market + white-label gap Gong structurally cannot serve |
| **Wing Venture Capital** | Early Gong backer; enterprise-AI focus | AI-native architecture vs. incumbents retrofitting gen-AI |
| **Norwest Venture Partners** | Gong investor; growth + early | Dual-engine model, channel leverage |
| **Scale Venture Partners** | GTM-SaaS specialists; "Scale Studio" benchmarks | Efficient CAC via channel; strong unit economics |
| **Costanoa Ventures** | B2B applied-AI seed | Workflow depth ("after the call"), not just transcription |

### 11.2 Applied-AI / B2B-SaaS seed specialists

| Investor | Why they fit |
|---|---|
| **Craft Ventures** | SaaS operators; sales/revenue tooling pattern-match |
| **Amplify Partners** | Technical/AI-infra seed; multi-model, pluggable-ASR story |
| **Boldstart Ventures** | Dev-first / API-first infra — fits the OEM/headless channel |
| **Base10 Partners** | "Automation for the real economy" — BPO/mid-market fit |
| **Bonfire Ventures** | B2B-SaaS seed specialists |
| **Point Nine Capital** | SaaS seed with SMB/white-label DNA (strong for EU expansion) |

### 11.3 Strategic / channel investors *(lead here — a check validates the channel)*

| Investor | Why they fit | Strategic value beyond capital |
|---|---|---|
| **Menlo Ventures — Anthology Fund (with Anthropic)** | Purpose-built for companies building on Claude — LINDA *is* built on Claude | Model access, Anthropic co-marketing, credibility |
| **Twilio Ventures** | CPaaS; LINDA rides Twilio's rails and lists on its marketplace | Channel + marketplace distribution |
| **HubSpot Ventures** | CRM the ICP already uses; App Marketplace | Co-sell + marketplace |
| **Salesforce Ventures** | AppExchange distribution; enterprise credibility | Marketplace + enterprise pull |
| **Zoom / strategic UCaaS funds** | Zoom Phone ingestion already built | Channel + app-marketplace |
| **Telstra Ventures** | Telecom-strategic; understands the UCaaS reseller channel | Warm intros to carrier/UCaaS distributors |

### 11.4 The ask (fill in the specifics)

- **Round / amount:** seed / seed-extension — sized to fund 1 Partner Manager + 1 growth
  hire (partner enablement or SMB growth, whichever motion is winning) + 1–2 engineers
  through the Phase 2 gate ($500K ARR, 2 live partners). *[Insert target raise and
  valuation.]*
- **Use of funds:** ~55% GTM (partner manager, enablement, channel events, SMB growth),
  ~30% engineering (CRM/email/Teams completion, partner portal hardening), ~15% G&A +
  compliance (SOC 2 audit renewal, CASA, HIPAA BAA readiness).
- **Milestones this round buys:** first 2 signed Elite/Premier distributors (incl. one
  wholesale-provider flagship); ~50 paying SMB customers via self-serve; verticalized
  reference customers in 2–3 target industries; repeatable 45-Day Preview close for
  stretch deals.
- **Why us, why now:** the product is built and multi-tenant-safe (technical risk retired);
  the market gap is structural (no one else is turnkey *and* white-label); and the channel that
  distributes it already exists and is actively shopping for AI parity.

---

## 12. Team

**Initial org (from the base plan):**

| Role | Priority | Rationale |
|---|---|---|
| Founding Engineer — Backend/AI | Critical | Core pipeline, AI analysis, API |
| Founding Engineer — Full-Stack | Critical | Dashboard, white-label theming, integrations |
| Founding Engineer — Infra/DevOps | High | Multi-tenant infra, CI/CD, security |
| Product Designer | High | Dashboard/onboarding/white-label UX |
| Head of Sales | High | Direct motion + partner development |
| Founding Engineer — Frontend | Medium | Analytics, embeddable widget |
| Customer Success | Medium | Onboarding, retention, feedback loop |
| Content Marketer | Medium | SEO, case studies, social |

*[Insert founder bios, prior exits, and domain credibility — investors weight this heavily
at seed. Emphasize any telecom/UCaaS-channel relationships and applied-AI depth.]*

---

## 13. Risks & Mitigation

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| Gong / incumbent launches SMB tier | High | Medium | Differentiate on white-label, speed-to-value, price transparency — and on channel reach an SMB tier alone doesn't buy |
| Claude API outage / price hike | High | Low | Single-catalog model routing + failover; multi-model optionality; volume pricing |
| Slow white-label sales cycles | Medium | High | Accepted as the cost of channel-first: SMB self-serve revenue is the independent floor while partner deals cycle; the program is build-complete so cycles start day one; mid-market direct opens in Phase 3 as a second hedge |
| Security incident (multi-tenant) | Critical | Low | RLS + Qdrant choke point, SOC 2, pen testing, bug bounty |
| High first-year churn | High | Medium | <24h time-to-value, onboarding investment, success team, NPS loop |
| Recording-consent regulation tightens | Medium | Medium | Built-in consent workflows, data-residency options, per-market legal review |
| Hyperscaler bundling / API vendors move up-stack | High | Medium | Workflow depth + channel lock-in + telephony-agnostic reach they can't match |

---

## 14. Appendix: Pricing & KPI Reference

### 14.1 Tier name mapping (marketing ↔ code)

| Marketing name | Canonical (code) name |
|---|---|
| Starter | Sandbox |
| Professional | Starter |
| Business | Growth |
| Enterprise | Enterprise |

*The public/marketing names are used throughout this document. `backend/app/plans.py`, the
SPA, and Stripe price IDs are the source of truth for the canonical names.*

### 14.2 KPI targets

| Metric | Year 1 | Year 2 |
|---|---|---|
| ARR | $500K | $3M |
| Paying customers | 100 | 400 |
| Live white-label partners | 2 | 10 |
| Net revenue retention | >110% | >120% |
| Gross margin | >70% | >78% |
| Free-to-paid conversion | 5–8% | — |
| Demo-to-close rate | 25% | — |
| AI action-item acceptance | >70% | — |
| Time-to-insight | <5 min | — |

### 14.3 Source documents

- [BUSINESS_PLAN.md](BUSINESS_PLAN.md) — full competitor deep-dives, SWOT, sales motions.
- [PRICING_MODELS.md](PRICING_MODELS.md) — cost-plus + competitor-anchored models, add-on
  SKUs, full white-label channel program.
- [ARCHITECTURE.md](../ARCHITECTURE.md) — technical system of record.
- `website/index.html` — customer-facing messaging and the 45-Day Preview motion.
- [scorecard_coaching_framework.md](scorecard_coaching_framework.md) — coaching methodology
  (a differentiation asset for the QA/coaching product).

---

*Placeholders in [brackets] — target raise, valuation, founder bios, specific named accounts —
are the founder's to fill. Everything else is synthesized from the repository's own product,
pricing, and strategy documentation.*
