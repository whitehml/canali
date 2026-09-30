# pRidge methodology

pRidge (prior ridge) rates FTC teams by regressing alliance scores on which teams played, shrunk toward each team's
pre-event [EPA](../../epa/docs/methodology.md). A team's pRidge is the points it is estimated to add to an alliance's
no-foul score, fitted within one event.

pRidge was designed and published for FRC by Krotkov et al. in [*Prior Ridge: Regularization for
FRC*](https://www.chiefdelphi.com/t/introducing-prior-ridge-regularization-for-frc-rating/519531), which is the primary
source for this model's implementation. The port to FTC deviates in fitting the [regularization parameter](#lambda).

## Data

### Response

The response is the non-foul score: an alliance's final score minus the foul points its opponent handed it.
`pub.v_match_rating_input` publishes it as `score_no_foul`. Each scoring [component](#components) is fitted on its own
response, and the total is fitted on the sum.

### Rows

A row is one alliance in one qualification match. Elimination matches are not used. A team that did not show up is
dropped from its row, so a row holds one or two teams. A team that is in no row has no rating.

Each event is solved as its own matrix. Its columns are the teams that played, sorted by team number, and its rows are
the alliances played up to a chosen match index.

### Rated events

| Tier | Event types |
| --- | --- |
| REGULAR | Qualifier, League Meet |
| RCMP | Super Qualifier, League Tournament, Championship |
| CMP | FIRST Championship |

Every qualification match in the event types listed above is used. Off-Season and Premier events are not used: they
frequently run their own rules, so their scores do not measure the season's game. Surrogate appearances and disqualified
teams are used, since the match and the score are real. No-shows are not.

## Estimator

For $n$ rows and $p$ teams, $X$ is the $n \times p$ matrix with a 1 where a team played on an alliance, $y$ is the
response, and $\beta_0$ is the prior, one value per team. The rating is

```math
\beta = (X^\top X + \lambda I)^{-1}(X^\top y + \lambda \beta_0)
```

The penalty $\lambda$ is worth $\lambda$ extra observations per team, all reporting the prior. The limits are the two
models it sits between:

| $\lambda$ | Rating |
| --- | --- |
| $\to 0$ | OPR, the least-squares fit, which needs $\operatorname{rank}(X) = p$ |
| $\to \infty$ | the prior, unchanged |

Where $\operatorname{rank}(X) = p$ the rating is a matrix-weighted average of OPR and the prior:

```math
\beta = (X^\top X + \lambda I)^{-1}\big(X^\top X\,\beta_{\text{OPR}} + \lambda\,\beta_0\big)
```

Any $\lambda > 0$ gives a rating, including early in an event when there are fewer rows than teams.

### Shrinkage

$X^\top X$ has a team's match count $m$ on its diagonal, so in a direction that separates two teams the prior carries
about $\lambda / (m + \lambda)$ of the weight.

| Matches played | Prior weight at $\lambda = 1.28$ |
| --- | --- |
| 1 | 56% |
| 2 | 39% |
| 5 | 20% |
| 8 | 14% |

An FTC team plays about 5 qualification matches at most events. The fit is mostly prior after the first match and mostly
data by the last, and ends an event close to OPR and to EPA. A bias in the prior is imported at the same weight.

The columns of $X$ are not standardized. They are 0/1 indicators, and $\beta$ is in points.

### Components

Each scoring component is fitted on its own response against its own prior, at the same $X$ and $\lambda$ as the total.
The estimator is linear in $y$ and $\beta_0$, so when the components sum to the total in both, the component ratings sum
to the total rating. The partition comes from the rule pack, the same one EPA reads.

## Prior

$\beta_0$ is a team's `epa_scaled` from its `pre_event` EPA row at the event, for the total and for each component. The
rows come from one completed EPA batch run, pinned by version, and the fit run records that version as its
`prior_version`. A team with no EPA row is an error and is not filled with a default.

Pre-event EPA is the only prior so far, and others may be tried. The current implementation only supports priors that:

- Are fixed across the event. One prior is loaded per event and used at every match index, and the
  [leave-one-out identity](loocv-derivation.md) depends on it not absorbing the event's own matches. Pre-event EPA
  satisfies this because it is computed before the event's first match.
- Supply a value per team for each scoring component as well as for the total, since every component is fitted
  against its own prior.

## Fit indices

A fit is taken at a match index $k$, the ordinal of a match within the event, using every qualification row up to and
including $k$. A season run fits each rated event once, at its last ordinal. A live run refits an event at every
ordinal, so a team's rating is available as the event unfolds. Every rating row carries its `as_of_match` and the
$\lambda$ it was fitted with.

## Lambda

One constant per tier, fixed across fits and seasons.

| Tier | $\lambda$ |
| --- | --- |
| REGULAR | 1.28 |
| RCMP, CMP | 1.93 |

The constant is the $\lambda$ that minimizes leave-one-out squared error, pooled across all seasons over every fit of a
tier's events whose $X$ has $\operatorname{rank}(X) = p$, at every match index. `tune.observe` computes it on a
log-spaced grid of 80 points from 0.4 to 4.0, and reports a 90% interval from resampling events, since fits within an
event share rows and are not independent.

| Tier | Events | Pooled minimizer | 90% interval | Leave-one-season-out picks |
| --- | --- | --- | --- | --- |
| REGULAR | 3,433 | 1.283 | 1.25 to 1.36 | 1.21 to 1.32 |
| RCMP and CMP | 815 | 1.930 | 1.77 to 2.11 | 1.82 to 2.11 |

### Why not select per fit

The paper selects $\lambda$ afresh for every fit by leave-one-out. On FTC data that approach is not necessarily any
better than a single fixed $\lambda$.

After $\operatorname{rank}(X) = p$, per-fit selection is still nearly indistinguishable from a fixed value. Choosing
$\lambda$ afresh for each fit with [`fit_event_per_fit`](../src/pridge/fit.py), from the same range of values as the
tier constants, is slightly worse than applying the tier's constant with [`fit_event`](../src/pridge/fit.py) in
regular-season events, until the final few matches of an event. Positive costs below mean per-fit selection is worse.

| Nth match | Cost of per-fit selection | Predictions | Events |
| --- | --- | --- | --- |
| 3rd | +1.9%, interval +1.2% to +2.5% | 22,760 | 3,273 |
| 4th | +0.4%, interval -0.1% to +1.0% | 25,740 | 3,412 |
| 5th | +0.2%, interval -0.2% to +0.6% | 30,084 | 3,431 |
| 6th or later | -0.3%, interval -1.0% to +0.5% | 9,134 | 1,301 |

At the championship tiers, RCMP and CMP together, the same comparison against the constant of 1.93:

| Fits | Cost of per-fit selection | Predictions | Events |
| --- | --- | --- | --- |
| Before full rank | +3.1%, interval +2.1% to +4.0% | 12,373 | 816 |
| After full rank | -0.2%, interval -0.6% to +0.2% | 37,527 | 816 |
| Last 3 match indices | -0.6%, interval -1.3% to +0.1% | 4,896 | 816 |

After full rank the two rules are indistinguishable.

### Why two constants

Later in the season, the prior is more reliable, at least for the population selected into RCMP and CMP events. A
somewhat higher $\lambda$ for those events is observed to give a moderate reduction in mean squared error.
