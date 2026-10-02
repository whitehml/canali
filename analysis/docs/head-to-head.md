# Head-to-head

pRidge, EPA and OPR are scored on the same alliances by next-match prediction error.

## Method

Each model predicts the non-foul score of every qualification alliance after the first match of an event, from the
ratings it held at the match before.

- EPA is each team's latest match-grain rating before the match.
- pRidge is refit on the matches up to that point, shrunk toward the same run's pre-event EPA, at the event tier's
  lambda.
- OPR is least squares on the matches up to that point, using the minimum-norm least-squares solution when
  rank-deficient.

A match is scored only if both alliances are fully rated by all three models.

A match whose breakdown does not add up to its official non-foul score is treated as unplayed by all three models: it is
neither fit nor scored, and it does not count toward any team's round.

Error is computed on predicted and actual non-foul scores. Two models are compared by the difference in mean squared
error over their shared rows, with a 95% interval from a percentile bootstrap that resamples whole events.
A table row built from too few events shows no interval. The minimum is `MIN_EVENTS_FOR_INTERVAL` in
`analysis/src/analysis/report.py`.

### Splits

- Match index: the i-th match of the event.
- Round: round n is a match where both alliance members are playing their nth match that the models learned from. An
  alliance whose members disagree has no round.
- Tier: the type of event a match took place at.

To reproduce, run `analysis` with the versions and seasons named below.

## Standing result

Measured on 2022 to 2025 with `epa-0.10.0` and `pridge-0.5.0`, at each tier's lambda from
`pridge/src/pridge/constants.py`. A positive pRidge-against-EPA value means pRidge is worse. Gaps are relative to EPA's
mean squared error, and intervals are on the difference, in points squared.

### Pooled

| Group | Rows | Events | pRidge | EPA | OPR[^1] | pRidge vs EPA |
| --- | --- | --- | --- | --- | --- | --- |
| All rated events | 170,770 | 4,249 | 854.5 | 840.1 | 1,598.6 | +1.7% (+11.1 to +17.7) |
| Regular season | 121,242 | 3,433 | 680.8 | 671.0 | 1,219.6 | +1.5% (+6.6 to +13.4) |
| Championship | 49,528 | 816 | 1,279.7 | 1,254.0 | 2,526.2 | +2.1% (+18.2 to +32.9) |

[^1]: OPR's error is inflated by rank deficiency early in each event, before there are enough matches to identify every
    team.

### By round

pRidge against EPA, by which match the alliance members are playing.

| Round | Regular rows | Regular gap | Championship rows | Championship gap |
| --- | --- | --- | --- | --- |
| 2 | 24,033 | +0.6% (-0.5 to +10.2) | 9,667 | +0.4% (-7.1 to +18.9) |
| 3 | 24,776 | +1.5% (+4.2 to +16.4) | 9,807 | +1.8% (+10.8 to +35.5) |
| 4 | 26,269 | +1.3% (+2.7 to +14.2) | 10,074 | +2.6% (+18.5 to +41.8) |
| 5 | 26,186 | +2.3% (+8.2 to +19.2) | 10,035 | +2.7% (+17.7 to +40.6) |
| 6 | 8,644 | +4.3% (+17.1 to +35.0) | 4,435 | +2.7% (+15.9 to +56.1) |
| 7 to 10 | none | none | 2,935 | +2.4% to +3.8%, intervals include zero from round 8 |

A round that few events reach shows no interval, which is why the latest round of each tier is not tabulated.

#### Both tiers combined, rounds 3 to 6

All three models on the same rows, regular season and championship together.

| Round | Rows | Events | pRidge | EPA | OPR | pRidge vs EPA | pRidge vs OPR | EPA vs OPR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 | 34,583 | 4,248 | 869.9 | 856.0 | 2,460.6 | +1.6% (+8.7 to +19.4) | -64.6% | -65.2% |
| 4 | 36,343 | 4,243 | 799.7 | 785.1 | 1,128.5 | +1.9% (+9.3 to +20.0) | -29.1% | -30.4% |
| 5 | 36,221 | 4,236 | 755.0 | 737.1 | 901.9 | +2.4% (+12.8 to +22.9) | -16.3% | -18.3% |
| 6 | 13,079 | 2,187 | 882.2 | 852.7 | 990.4 | +3.5% (+21.1 to +38.1) | -10.9% | -13.9% |

OPR's error dramatically decreases once the matrix is no longer rank-deficient.

### By match index

pRidge against EPA at selected indices.

| Index | Regular rows | Regular gap | Championship rows | Championship gap |
| --- | --- | --- | --- | --- |
| 5 | 5,208 | -0.1% (-9.9 to +9.4) | 464 | +1.9% (-15.3 to +47.1) |
| 10 | 6,394 | -0.2% (-10.4 to +7.9) | 1,518 | +0.2% (-22.5 to +26.0) |
| 15 | 4,890 | +1.9% (+0.7 to +22.9) | 1,550 | +2.9% (+2.4 to +60.6) |
| 20 | 3,090 | +2.1% (-0.9 to +29.1) | 1,428 | -0.3% (-35.5 to +28.4) |
| 25 | 2,012 | +3.2% (+3.2 to +45.1) | 1,226 | +1.4% (-13.8 to +45.3) |
| 30 | 1,138 | +5.2% (+7.4 to +59.1) | 918 | +2.6% (-7.4 to +61.3) |

### 2025

| Group | Rows | Events | pRidge vs EPA |
| --- | --- | --- | --- |
| All rated events | 49,990 | 1,177 | +0.1% (-2.8 to +5.0) |
| Regular season | 34,760 | 939 | -0.4% (-5.8 to +2.3) |
| Championship | 15,230 | 238 | +0.7% (-2.4 to +16.8) |

By round in the 2025 regular season, every pRidge-against-EPA interval includes zero.

#### Regular season by round, all three models

| Round | Rows | Events | pRidge | EPA | OPR | pRidge vs EPA | pRidge vs OPR | EPA vs OPR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 6,951 | 927 | 589.0 | 590.9 | 1,212.4 | -0.3% (-8.2 to +5.0) | -51.4% | -51.3% |
| 3 | 7,136 | 938 | 511.5 | 507.5 | 1,320.2 | +0.8% (-3.9 to +12.4) | -61.3% | -61.6% |
| 4 | 7,533 | 937 | 453.2 | 456.8 | 623.3 | -0.8% (-10.5 to +3.0) | -27.3% | -26.7% |
| 5 | 7,504 | 935 | 427.8 | 433.3 | 498.2 | -1.3% (-12.3 to +1.3) | -14.1% | -13.0% |
| 6 | 2,514 | 481 | 455.2 | 446.3 | 497.5 | +2.0% (-1.3 to +18.9) | -8.5% | -10.3% |
