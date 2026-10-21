# Hosting cost estimate: Pennsylvania DECODE season

Estimated October 21, 2026, using the [Cloudflare forecast benchmarks](forecast-benchmark-2026-10-21.md) and the
2025–2026 Pennsylvania competition schedule. All amounts are USD. This applies October 2026 prices to a historical
workload; it is not a historical invoice or a spending guarantee.

The [worldwide DECODE estimate](worldwide-hosting-cost-estimate.md) extrapolates this workload to FIRST's published
worldwide event catalog, with separate coverage and scaling assumptions.

## Expected cost

The forecast workload is expected to fit within Workers Paid's included usage. The subscription therefore accounts
for most of the projected Cloudflare bill, assuming other account workloads leave those allowances available.

| Period with Workers Paid enabled | Subscription and estimated forecast usage | Suggested budget with headroom |
| --- | ---: | ---: |
| Competition months: December–March | $20 | $25 |
| Kickoff through state championship: September–March | $35 | $40 |
| Full year | $60 | — |

If the account already pays for Workers and has spare included usage, the incremental Cloudflare cost of these
forecasts could be approximately zero. Public visitors receive static assets and do not trigger forecast computation.
The suggested budgets exclude external hosting for the native importer and publisher.

## Workload assumptions

The intended operating model polls for updates every minute during competition hours and recomputes a forecast
only when its inputs change. A poll that finds no changes starts no simulation and produces no new publication.
New match results are the main trigger; corrected results, revised schedules, alliance selection, model updates,
and changed advancement assumptions can also require a new forecast. Polling frequency therefore does not determine
the number of simulations. Polling overhead is excluded from the measured per-forecast cost.

Pennsylvania held [12 DECODE qualifiers](https://www.ftcpenn.org/email-archive/21928) and its
[state championship on March 14–15, 2026](https://www.ftcpenn.org/ftc-events/2025-2026-season/pennsylvania-ftc-championship).
Official qualification schedules cached in the repository workspace and FIRST playoff schedules retrieved October 21
contain **464 qualification matches and 116 playoff matches**, including tied games, across these 13 events.

The baseline recomputes the active event:

- Once before matches begin.
- After every qualification and playoff result.
- Once after alliance selection.

That produces `464 + 116 + (2 × 13) = 606` forecasts. Corrections and other input changes would add forecasts;
multiple updates received while a forecast runs can be coalesced into the next forecast of the latest state.
The estimate does not refresh every future event after every result at another event.

## Benchmark basis

Two hosted cases covered a 24-team qualification snapshot and a 40-team playoff snapshot. They used an absolute
Monte Carlo standard-error target of 1 percentage point, a 1,000-simulation minimum, and a 2,501-simulation maximum.
Both completed without retries and exactly matched native results.

Including assembly, dispatch, export, and half of the shared setup per snapshot, measured CPU averaged
**36.09 seconds per forecast**. Workflow execution used 78–79 steps per forecast. The projection applies that average
to all events; it is not a measurement of every event or every stage. The benchmark's import/replay checks used
an already-current season corpus, so a large season rebuild is outside the measured average.

## Monthly projection

| Month | Events | Qualification matches | Playoff matches | Forecasts | Estimated CPU hours | Subscription + forecast overage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| December 2025 | 2 | 68 | 20 | 92 | 0.92 | $5.00 |
| January 2026 | 5 | 134 | 34 | 178 | 1.78 | $5.00 |
| February 2026 | 5 | 212 | 52 | 274 | 2.75 | $5.00 |
| March 2026 | 1 | 50 | 10 | 62 | 0.62 | $5.00 |
| **Total** | **13** | **464** | **116** | **606** | **6.08** | **$20.00** |

The entire season's forecast CPU is below one month's included 8.33 CPU hours. The busiest month has approximately
three times the estimated forecast CPU requirement available before exceeding that allowance. Calendar months are
used for illustration; actual billing periods follow the subscription renewal date.

The season would use approximately 48,000 Workflow steps, 2.1 million D1 row reads, and 54,000 row writes at the
measured per-snapshot rates. These totals are well below the corresponding monthly included allowances. Polling
and dispatch add overhead outside the snapshot count; restricting them to competition days leaves substantial headroom.

## Pricing and calculation

Published Cloudflare rates used in this estimate:

| Resource | Monthly included usage on Workers Paid | Overage rate |
| --- | --- | --- |
| Workers subscription | — | $5/month base charge |
| CPU | 30 million milliseconds | $0.02 per million milliseconds |
| Requests | 10 million | $0.30 per million requests |
| Workflow steps | 500,000 | $0.80 per 100,000 steps |
| Workflow storage | 1 GB-month | $0.20 per GB-month |
| D1 reads | 25 billion rows | $0.001 per million rows |
| D1 writes | 50 million rows | $1 per million rows |
| D1 storage | 5 GB | $0.75 per GB-month |

Sources: [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/),
[Workflow pricing](https://developers.cloudflare.com/workflows/reference/pricing/), and
[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).

CPU hours are `forecasts × 36.090356 / 3,600`. Monthly CPU overage is
`max(0, forecasts × 36,090.356 − 30,000,000) / 1,000,000 × $0.02`.
Workers CPU measurements already include Workflow CPU; adding the two datasets would double-count computation.
Retries consume CPU, although they do not add billable Workflow steps. A CPU limit is not a monthly spending cap.
The forecast retry policy disables automatic simulation-step retries. A failed event remains blocked across new
match results and engine changes until an operator explicitly requests recovery. Recovery resumes a matching durable
checkpoint, avoiding repetition of committed batches. See the [operator recovery instructions](../worker/README.md#unattended-forecast-computation).

## Sensitivity and exclusions

As a hypothetical stress case, recomputing every minute even without changed inputs for eight hours on each of
the 14 event days would produce
6,720 forecasts and approximately 67.37 CPU hours. Spread across the four competition months, that projects to
$2.45 in CPU overage, or **$22.45 including subscriptions**. No month would exceed the Workflow-step allowance.
This deliberately redundant workload is outside the intended operating model. Polling every minute while computing
only changed inputs uses the 606-forecast baseline above, plus polling overhead.

The projection excludes scrimmages, off-season events, a large season rebuild, external hosting for the native
importer/publisher, and unmeasured polling and deployment costs. Logging, storage growth, corrections, tighter
convergence targets, and unrelated account workloads can also change the bill. Public traffic remains isolated
from metered computation by static publication.

Raw season counts, retrieved playoff schedules, and calculation code are archived locally under
`var/worker-benchmark/pa-decode-cost-20261021/`. The benchmark report documents the measured cases and the locations
of their raw artifacts. These ignored workspace artifacts are not required to read this estimate.
