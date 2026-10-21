# Hosting cost estimate: worldwide DECODE season

Estimated October 21, 2026, using the [Cloudflare forecast benchmarks](forecast-benchmark-2026-10-21.md),
the [Pennsylvania workload estimate](hosting-cost-estimate.md), and FIRST's 2025–2026 worldwide event catalog.
All amounts are USD. This applies October 2026 prices to a historical workload; it is not a historical invoice
or a spending guarantee.

## Expected cost

Scaling Pennsylvania's forecasts per event to the published worldwide catalog projects approximately **$106–$120
for the season** in Workers subscription, CPU, and Workflow-step charges. Keeping the subscription enabled for
a full year increases that subtotal to approximately **$116–$125**. This assumes other account workloads leave
the monthly included allowances available.

| Projected resource | Competition entries | Including non-advancement and off-season entries |
| --- | ---: | ---: |
| Published catalog entries | 1,276 | 1,467 |
| Forecasts | 59,481 | 68,385 |
| CPU hours | 596 | 686 |
| Subscription during active months | $50.00 | $55.00 |
| CPU overage after monthly allowances | $38.12 | $43.46 |
| Workflow-step overage after monthly allowances | $18.32 | $21.47 |
| **Season subtotal** | **$106.44** | **$119.93** |
| **Subtotal with subscription enabled all year** | **$116.44** | **$124.93** |

A provisional **$200–$300 Cloudflare allowance** provides headroom around this extrapolation. It is a planning
judgment, not a measured upper bound. External hosting for the native importer and publisher needs a separate budget.

Public visitors receive static assets and do not initiate forecast computation. Cloudflare currently charges
nothing for static asset requests or asset storage; requests to dynamic Worker code follow Workers pricing.
See [static asset billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/).

## Event coverage and workload assumptions

The official FIRST API's `/2025/events` catalog, retrieved October 21, contained these published competition entries:

| Event type | Entries |
| --- | ---: |
| League Meet | 602 |
| Qualifier | 358 |
| League Tournament | 128 |
| Super Qualifier | 11 |
| Championship | 145 |
| Premier | 25 |
| FIRST Championship | 7 |
| **Total** | **1,276** |

These competition entries span 42 countries. The broader scenario adds 171 published Non-Advancement entries and
20 published Off-Season entries. Both scenarios exclude unpublished entries. The catalog includes individual
divisions as well as their parent events, so these are counts of competition entries, not distinct venues or
weekends. Coverage is limited to events listed and published in FIRST's service.
See the [FIRST event service](https://ftc-events.firstinspires.org/services).

Pennsylvania's baseline contains 606 forecasts across 13 events: one before matches, one after each qualification
and playoff result, and one after alliance selection. This projection applies `606 / 13`, approximately 46.6
forecasts, to each worldwide entry. **Worldwide match totals were not counted.** The decimal forecast counts used
in calculations are expected workload averages, rounded to whole forecasts in the summary.

The intended service polls every minute during competition hours and recomputes only when inputs change.
An unchanged poll starts no simulation and produces no publication. The estimate updates the affected event;
it does not refresh every future event worldwide after every match result. Corrections, revised schedules,
model changes, and other changed inputs can add forecasts. Updates arriving during a run can be coalesced into
the next forecast of the latest state.

## Benchmark basis and calculation

The two hosted benchmarks used a 1-percentage-point absolute Monte Carlo standard-error target, a 1,000-simulation
minimum, and a 2,501-simulation maximum. They covered a 24-team qualification snapshot and a 40-team playoff
snapshot, completed without retries, and exactly matched native results. Including assembly, dispatch, export,
and half the shared setup per forecast, their average was **36.090356 CPU seconds per forecast**. This estimate
uses 79 Workflow steps per forecast, the higher of the two measured counts.

For each calendar month, let `E` be the number of included entries whose start date falls in that month:

```text
Forecasts F = E × 606 / 13
CPU hours = F × 36.090356 / 3,600
CPU overage = max(0, F × 36,090.356 − 30,000,000) / 1,000,000 × $0.02
Workflow-step overage = max(0, F × 79 − 500,000) / 100,000 × $0.80
Monthly subtotal = $5 + CPU overage + Workflow-step overage
```

Monthly allowances reset independently; unused allowance in a quiet month does not offset a busy month.
Calendar months approximate billing periods, which actually follow the subscription renewal date. A multi-day
entry's projected work is assigned to its start month. Workers CPU already includes Workflow CPU; adding them
would double-count computation.

The subscription and CPU rates come from [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/).
Step rates come from [Workflow pricing](https://developers.cloudflare.com/workflows/reference/pricing/).
These are October 2026 rates, including Workflow-step billing, rather than the rates necessarily in effect when
each historical event took place.

## Monthly projection

| Month | Competition entries | Competition subtotal | Entries including non-advancement and off-season | Broader subtotal |
| --- | ---: | ---: | ---: | ---: |
| October 2025 | 11 | $5.00 | 40 | $5.75 |
| November 2025 | 329 | $21.16 | 378 | $24.25 |
| December 2025 | 268 | $17.31 | 299 | $19.27 |
| January 2026 | 328 | $21.10 | 343 | $22.05 |
| February 2026 | 240 | $15.55 | 252 | $16.30 |
| March 2026 | 57 | $6.32 | 67 | $6.65 |
| April 2026 | 14 | $5.00 | 27 | $5.31 |
| May 2026 | 10 | $5.00 | 27 | $5.31 |
| June 2026 | 13 | $5.00 | 19 | $5.04 |
| July 2026 | 6 | $5.00 | 11 | $5.00 |
| August 2026 | 0 | — | 4 | $5.00 |
| **Total** | **1,276** | **$106.44** | **1,467** | **$119.93** |

Totals use unrounded calculations. The competition scenario assumes the subscription is disabled outside its
ten active months; the broader scenario has eleven active months. A year-round subscription costs $60.

## Uncertainty and work outside the estimate

The main uncertainty is applying Pennsylvania's event mix and two measured snapshot costs worldwide. League
meets can involve less work, while larger championships can require more. Parent events and divisions have
different workloads. This estimate does not adjust forecasts or CPU per entry for those differences.

The subtotal excludes unmeasured worldwide ingestion and season replay, polling, requests, logging, D1 and
Workflow storage, and publication/build/deployment costs. The benchmark imported an already-current small corpus;
it did not measure rebuilding worldwide history. Measured D1 reads and writes scale to roughly 202–232 million
reads and 5.3–6.1 million writes across the season, below a single month's paid allowances, but worldwide ingestion
and larger query scans are outside those measurements. See [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).

Worldwide operation still needs validation of concurrent-event throughput, larger-event CPU requirements,
database and checkpoint retention, and publication frequency. Successful execution of two sequential snapshots
does not establish those limits. The forecast retry policy disables automatic simulation-step retries and blocks
failed events until explicit operator recovery; the estimate includes no recovery runs or repeated failed work.
Tighter convergence targets, additional forecast triggers, and unrelated account usage can increase the bill.

The retrieved catalog and calculated monthly estimates are archived locally under
`var/worker-benchmark/worldwide-decode-cost-20261021/`. These ignored workspace artifacts are not required to read
or reproduce the calculation: the event counts, monthly distribution, benchmark inputs, and formulas appear above.
