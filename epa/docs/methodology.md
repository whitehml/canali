# EPA methodology

EPA (Expected Points Added) is an Elo-shaped rating for FTC teams, measured in the season's own points. A team's EPA is
what it is expected to add to an alliance's non-foul score.

EPA was designed and published by [Statbotics](https://www.statbotics.io/) for FRC, and its write-up,
[Expected Points Added](https://www.statbotics.io/blog/epa), is the primary source for this model's implementation.
The port to FTC hews closely to the reference but includes minor deviations, detailed below.

## Data

### Response

The response is the non-foul score: an alliance's final score minus the foul points its opponent handed it.
`pub.v_match_rating_input` publishes it as `score_no_foul`. The model is fitted and updated on this score's individual
[components](#components), and the total is their sum.

### Order of play

Events are consumed in `pub.v_event_sequence` order, by `date_start` and then `event_code`. Matches within an event are
consumed in published ordinal order.

### Rated events

| Tier | Event types |
| --- | --- |
| REGULAR | Qualifier, League Meet |
| RCMP | Super Qualifier, League Tournament, Championship |
| CMP | FIRST Championship |

Every match in the event types listed above is rated. Off-Season and Premier events are not rated: they frequently run
their own rules, so their scores do not measure the season's game. Surrogate appearances and disqualified teams are
rated, since the match and the score are real. No-shows are not.

A playoff match updates ratings at one third the weight of a qualification match but does not advance the
matches-played count that K depends on.

Constants are fitted only on qualification matches across all rated events.

## Rating

An alliance's prediction is the sum of its teams' EPAs. K is the learning rate, and M is how much of the opponent's
residual is subtracted from the alliance's own. Both are piecewise functions of qualification matches played; each
alliance reads them at the integer mean of its two teams' counts.

After a match, each team on an alliance moves by the same amount:

```python
predicted = epa[t1] + epa[t2]
residual = ((actual - predicted) - M * (opponent_actual - opponent_predicted)) / (1 + M)
change = (K / 2) * residual
```

The update target is not zero-sum: both alliances rise when both beat their predictions. In every version so far,
`M = 0` and the residual simplifies to:

```python
residual = actual - predicted
```

### Components

EPA is calculated on the individual score breakdown components, and a team's total EPA is the sum of its components.
The components are based on what the FIRST API provides, then hand-annotated as necessary in `rule_packs`, the `.toml`
files that define each season. The partition EPA fits is the season's `leaf` group, its finest-grain scoring line items,
each tagged as auto or teleop. `pub.v_phase_points_<season>` sums them back into an auto and a teleop score.

- Each component has its own rating for every team.
- After a match, each component is adjusted by how far the alliance's real score in that part missed its prediction.
- Every component uses the same K and M for now.

### Layoff

Before a team's `pre_event` row is written, it is credited with improvement for the time since it last competed,
measured from event start date to event start date. A team that has not yet competed that season earns nothing; the
bonus is only for teams iterating on field-tested designs. Teams that first compete later in the season are already
accounted for by the rookie EPA value. The boost is split across the team's components in proportion to their positive
values.

## Starting a season

At the end of each season, each team's final EPA is ranked within that season's field and mapped onto a right-skewed
distribution with mean 1500 and standard deviation 250, where about 1800 is the top 1%. Apart from the skew, the shape
of this distribution is arbitrary; the values are inherited from Statbotics. The mapped value is the team's `norm` for
that season.

A team's starting EPA is built from its two previous seasons, using the carryover parameters `w` and `reversion`
(see [Carryover](#carryover)):

```
previous = w * norm[last season] + (1 - w) * norm[season before]
start    = (1 - reversion) * previous + reversion * rookie_norm
```

- Returning teams: blend their last two seasons and revert part of the way toward the rookie start. The result is
  converted through the init scale (below) and floored at zero.
- Rookies: start at `rookie_norm`, 1450, which is 0.2 standard deviations below the field. Rookies are 27 to 31% of
  every season, and starting them at the field's mean is a known bias.
- Teams that skip a season: keep their older season at weight `1 - w` rather than restarting as a rookie. A missing
  season contributes the rookie value.

### Init scale

The `init_scale` is the mean and standard deviation of the season's first `INIT_WINDOW` alliance-rows in event order.
Until the window fills, the `init_scale` is borrowed from the average of all past seasons' `init_scale`s, and every row
produced under it carries `scale_provisional`.

The window fills 12 to 36 days into the season, by mid-November in every season.

| Season | Init mu | Init sigma |
| --- | --- | --- |
| 2022 | 50.3 | 34.9 |
| 2023 | 40.7 | 30.2 |
| 2024 | 46.9 | 35.0 |
| 2025 | 47.7 | 29.0 |

### Season scale

The `season_scale` is the mean and standard deviation of the alliance non-foul scores in the warehouse for the
season. For a season still in progress it is the running figure. The model does not use it. It is published for
analysis.

| Season | Season mu | Season sigma |
| --- | --- | --- |
| 2022 | 75.4 | 49.8 |
| 2023 | 68.7 | 53.7 |
| 2024 | 88.0 | 68.5 |
| 2025 | 71.9 | 47.1 |

## Constants

| Constant | Value |
| --- | --- |
| `FITTED_K` | 0.7, then 0.5 from 4 matches played, then 0.35 from 8 |
| `FITTED_M` | 0 |
| `FITTED_CARRYOVER` | w 0.7777, reversion 0.4325 |
| `FITTED_LAYOFF` | 0.15 initial scale sigma per 30 days, capped at 60 days |
| `ELIM_WEIGHT` | 1/3 |
| `INIT_WINDOW` | 4,000 alliance-rows |

### K

Both breakpoints and values were searched; the best breakpoints roughly match event boundaries.

| Breakpoints | Values | Pooled MSE |
| --- | --- | --- |
| (6, 12) | 0.7, 0.5, 0.25 | 783.00 |
| (6, 12) | 0.7, 0.5, 0.35 | 780.42 |
| (4, 8) | 0.7, 0.5, 0.35 | 774.68 |
| none | 0.5 | 795.24 |

### M

M = 0 beats every positive value, and remains best at every breakpoint set searched. This makes each alliance's rating
functionally independent.

### Carryover

Fitted by regressing teams' early-season scoring on their two prior seasons.

- `w` = 0.78: last season counts about 3.5 times the season before.
- Reversion = 0.43: a team keeps about 57% of its old standing, 44% from last season and 13% from the one before.

### Elimination weight

Elimination matches were weighted by each value below, swept over a grid and scored on qualification rows only:

| Weight | Pooled | 2022 | 2023 | 2024 | 2025 |
| --- | --- | --- | --- | --- | --- |
| 0, bracket discarded | 913.72 | 895.8 | 846.1 | 1246.7 | 666.3 |
| 1/4 | 909.59 | 891.4 | 842.3 | 1240.8 | 663.9 |
| 1/3 | 909.23 | 890.8 | 841.9 | 1240.5 | 663.7 |
| 1/2 | 909.44 | 890.4 | 842.0 | 1241.3 | 664.0 |
| 1, same as a qualification match | 913.93 | 893.5 | 846.3 | 1248.8 | 667.1 |

1/3 is Statbotics' value, and our search yielded no reason to overturn it.
