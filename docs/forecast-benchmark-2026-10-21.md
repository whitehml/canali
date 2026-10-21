# Forecast convergence and hosted execution

Local calibration on October 21, 2026 used the production snapshot assembler, simulation batches and convergence
code. Both cases used a 1,000-simulation minimum. The qualification case included the first six official matches
of 2025 USNJCMPPKWY (24 teams). The playoff case included all qualifications and the first two playoff matches of
2025 USPACMP (40 teams). The model coefficients and limited season history make these execution benchmarks,
rather than assessments of prediction accuracy.

| Absolute standard-error target | Qualification simulations | Local elapsed time | Playoff simulations | Local elapsed time |
| --- | ---: | ---: | ---: | ---: |
| 4 percentage points | 1,000 | 4.8 s | 1,000 | 5.1 s |
| 2 percentage points | 1,000 | 4.8 s | 1,000 | 5.1 s |
| 1 percentage point | 2,500 | 12.0 s | 2,496 | 13.1 s |
| 0.5 percentage points | 9,999 | 47.9 s | 9,980 | 51.5 s |

Targets reused a single random stream and checkpoint per case; elapsed times are cumulative from the start of
that case. The qualification result at the 1-percentage-point target exactly matched the completed hosted result.
Local elapsed time is not Cloudflare CPU time and must not be substituted into a hosted cost calculation.

The 1-percentage-point target is a reasonable baseline: a 2,501-simulation maximum is sufficient under the current
convergence criterion. A 2-percentage-point target uses about 60% fewer simulations in these cases. A
0.5-percentage-point target needs approximately four times the baseline work and can require 10,001 simulations
because the convergence comparison is strict. The target measures sampling noise in the tracked probabilities;
it does not establish forecast accuracy or a simultaneous confidence interval for every displayed probability.

## Hosted measurements

Two sequential runs used the calibrated 1-percentage-point target, a 1,000-simulation minimum and a 2,501-simulation
maximum. The private benchmark deployment was `47fe81f6-3f2c-4430-b08a-e51f8b34035a`, with engine
`4f56359b23bfffdb27d0598c61fd96120577ffa4f03f4f632eca40bcbee84755` and a 300,000 ms CPU limit.

| Measurement | 24-team qualifications | 40-team playoffs |
| --- | ---: | ---: |
| Simulations | 2,500 | 2,496 |
| Workflow steps | 79 | 78 |
| Workflow CPU time | 35.148 s | 35.895 s |
| Workflow elapsed time | 53.408 s | 53.244 s |
| Backend CPU including assembly, dispatch and export | 35.455 s | 36.312 s |
| D1 rows read | 3,905 | 2,865 |
| D1 rows written | 86 | 86 |
| Failed step attempts / retries | 0 | 0 |
| Exact native-result match | Yes | Yes |

The qualification snapshot included match 6. The playoff snapshot included the first two official playoff matches,
ending at series 2, match 1. Repeated dispatch returned the same instance for each event. Both assembled snapshots
were computed by durable Workflows and exported through the private endpoint. Neither computation was driven by
repeated HTTP `/step` calls.

Workflow CPU comes from individual invocation traces and agrees with the Workflow analytics CPU totals. Whole-path
CPU and D1 usage come from Cloudflare's GraphQL analytics over the recorded execution windows. These are usage
measurements, not an invoice. Analytics initially lagged and were collected again after completion. The final playoff
HTTP export trace was truncated when the tail session stopped; its usage is included in the later analytics total.
Workers analytics already includes Workflow CPU, so adding the two CPU datasets would double-count computation.

Shared setup consumed another 0.413 CPU seconds, bringing the two-snapshot total to approximately **72.181 CPU
seconds**, 6,783 D1 rows read and 179 rows written. Setup reimported identical cached FIRST payloads and verified the
season replay cursor was already current. The stored three-event corpus was replayed in the earlier trial; this
repeat measures incremental operation, not rebuilding a complete season. FIRST polling, a large season rebuild,
frontend deployment and concurrent-event load are outside these measurements. Public visitors still use static assets.

## CPU limits and cost

An earlier qualification run used the five-second CPU cap and completed in 160.5 elapsed seconds, with eight failed
step attempts across five steps. Checkpoints recovered, but CPU interruptions added retries and delay. With the
revised cap, both runs finished without interruptions. Raising a limit does not reserve or automatically charge
its full value; failed attempts still consume CPU, and the limit does not provide a monthly spending cap. Hosted
cron dispatch remains disabled and both benchmark jobs are complete.

At the published [Workers prices](https://developers.cloudflare.com/workers/platform/pricing/), 30 million CPU
milliseconds are included per month and additional CPU costs $0.02 per million milliseconds. Including half of
shared setup per snapshot, these cases averaged approximately 36,090 CPU milliseconds:

- Approximately 831 similar snapshots would fit the monthly CPU allowance if no other work consumed it.
- CPU beyond that allowance costs approximately $0.000722 per snapshot, or $0.72 per 1,000 snapshots.
- At 1,000 similar snapshots per month, CPU overage would be approximately $0.12 above the $5 Workers subscription,
  assuming the account's included allowances are otherwise available.

[Workflow pricing](https://developers.cloudflare.com/workflows/reference/pricing/) additionally includes 500,000
monthly steps, then charges $0.80 per 100,000 steps. Roughly 79 steps add $0.000632 per snapshot after that allowance.
Retries do not add billable steps, although their CPU time is billed. [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/)
includes 25 billion monthly reads and 50 million writes; the measured row volumes are small relative to those
allowances. Storage, logging, request usage and other account workloads must also be included in a full service budget.

Original trial and local calibration artifacts are archived under `var/worker-benchmark/e2e-20261021/`. Calibrated
hosted snapshots, exports, native comparisons, sanitized invocation traces, analytics and cost calculations are under
`var/worker-benchmark/e2e-calibrated-20261021/`. Credentials are excluded from the reports.

## Python simulation optimization

Local validation on October 21 compared the optimized implementation with the simulator from commit `b373cc5`.
It used the same frozen 24-team qualification and 40-team playoff snapshots as the hosted benchmarks, including
their original 1-percentage-point target, 1,000-simulation minimum and 2,501-simulation maximum. Both versions
round-tripped JSON checkpoints after every batch. Each measurement below averages two runs, with version order
reversed on the second repetition. Timings exclude imports and snapshot validation.

| Case | Simulations to convergence | Original elapsed | Optimized elapsed | Elapsed reduction | Original process CPU | Optimized process CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 24-team qualifications | 2,500 | 11.35 s | 7.93 s | 30.1% | 11.22 s | 7.92 s |
| 40-team playoffs | 2,496 | 11.91 s | 6.21 s | 47.8% | 11.80 s | 6.21 s |

The final reports, accumulated counts and RNG states matched exactly across both versions and repetitions.
Resuming the optimized engine from the original implementation's 1,000-trial checkpoints also produced the exact
original final reports and checkpoints. These are full converged runs; the earlier exploratory caching experiment
used fixed 1,000-trial runs.

A packaged-engine browser check at 1,000 qualification trials also exactly matched the native report, including
every team's probabilities and ranks, the iteration count and standard error.

The changes reuse fixed alliance predictions and schedule relationships, cache qualification tiebreakers only when
all qualifications are played, and avoid rebuilding award-history features and per-field normalization/seat lookups.
Caches are local to a run or checkpoint batch, invalidated when their input objects change, and excluded from durable
state. The models, iteration budgets, convergence checks, random draws and checkpoint format are unchanged.

These native measurements establish neither Cloudflare CPU savings nor browser speedups. The hosted measurements
and hosting projections above remain based on the preceding implementation until remeasured. Browser runtime
downloads, ingestion and publication are outside these timings. Engine source hashes change with the optimization;
deployment must use matching backend and browser sources and recompute forecasts for that engine before publication.

Profiles, the saved pre-change reference implementation, per-run reports and checkpoints, and comparison scripts are
archived locally under `var/python-performance-20261021/`, with production comparisons in its `production/` directory.
No hosted simulation was run for this optimization validation.

## Pennsylvania DECODE season projection

The [hosting cost estimate](hosting-cost-estimate.md) applies these measurements to Pennsylvania's 2025–2026
DECODE season, including the event counts, publication-frequency assumptions, monthly projection, pricing, and exclusions.
