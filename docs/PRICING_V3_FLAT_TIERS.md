# LINDA Pricing v3 — Flat-Tier Model (Owner-Approved)

**Status:** Approved direction as of 2026-08-06 (session with business owner).
**Supersedes:** the per-seat band tables in `PRICING_MODELS.md` §0.1/§6 and the tier
structure they imply. The white-label §9 partner program was separately rejected in
favor of the simple three-option model in `BUSINESS_PLAN.md` §6.2 (rev-share 70/30,
platform license, OEM per-call).
**Note:** the shipped `sandbox` (3-seat) tier in `backend/app/plans.py` was never
approved by the owner and is slated for removal when this model is implemented.

## Tier structure

Four tiers, **flat-priced per tier** (not per seat). A client pays the tier price
whether they run the minimum or maximum seats in the band.

| Tier | Seat band | Flat price / mo | Worst-case COGS / mo | Margin floor |
|---|---|---|---|---|
| **Startup** | 1–10 | **$749** | $140–180 | 76–81% |
| **Growth** | 11–25 | **$2,749** | $550–675 | 75–80% |
| **Expansion** | 26–50 | **$8,249** | $1,600–2,050 | 75–81% |
| **Enterprise** | 50+ | **Custom, unlisted** — per-seat + feature-based quote; internal floor ≈ 4× seat cost (≈$240/seat-equivalent) | $2,250–3,000 at 50 seats, +$45–60/seat beyond | ~75% by construction |

## Usage

- **Base cap: 2,000 minutes per seat per month** (all tiers).
- **Overage: $0.02/min** above the cap, trued up monthly.
- Because overage is billed, the "margin floor" column is a true floor — usage
  cannot erode it.

## Seats above the band

Extra seats are available without a tier upgrade at a set add-on price:

| Tier | Extra seat / mo |
|---|---|
| Startup | +$79 |
| Growth | +$119 |
| Expansion | +$179 |
| Enterprise | scales by contract |

Add-on pricing is set so stacking ~5 extra seats crosses the next tier's
economics — smoothing the band-edge cliff while still nudging upgrades.

## Pricing logic (for future revisions)

- Flat price = worst-case COGS (max seats × max usage at that tier's feature
  routing) ÷ 0.25, rounded to a sellable number.
- COGS basis: cost model in `PRICING_MODELS.md` §1.2 at 2,000 min/seat/mo.
  Low end = Whisper/batch + Haiku-first + cached; high end = Deepgram streaming +
  Sonnet/Opus routing.
- Anchors that matter when adjusting numbers: price ≥ 4× worst-case cost per tier;
  extra-seat price positioned so ~5 add-ons ≈ next tier.

## Feature gating

Each tier unlocks additional platform capability (feature-gated, not usage-gated).
The canonical feature ladder lives alongside this doc and maps to
`backend/app/plans.py` feature flags; the plans.py migration (rename tiers, remove
sandbox, add Expansion band, re-gate flags) is a separate implementation task
requiring owner sign-off on the final feature matrix.
