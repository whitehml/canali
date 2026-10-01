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
A table row built from fewer than twelve events shows no interval.

### Splits

- Match index: the i-th match of the event.
- Round: a match where both alliance members had exactly r previous matches that the models learned from. An alliance
  whose members disagree has no round.
- Tier: the type of event a match took place at.

To reproduce, run `analysis` with the versions and seasons named below.

## Standing result

Measured on 2022 to 2025 with `epa-0.9.0` and `pridge-0.4.0`, whose lambdas are 1.28 for the regular season and 1.93 for
the championship tiers. A positive pRidge-against-EPA value means pRidge is worse. Gaps are relative to EPA's mean
squared error, and intervals are on the difference, in points squared.

### Pooled

| Group | Rows | Events | pRidge | EPA | OPR[^1] | pRidge vs EPA |
| --- | --- | --- | --- | --- | --- | --- |
| All rated events | 170,774 | 4,249 | 854.5 | 840.1 | 1,598.6 | +1.7% (+11.2 to +17.7) |
| Regular season | 121,246 | 3,433 | 680.9 | 671.0 | 1,219.6 | +1.5% (+6.6 to +13.5) |
| Championship | 49,528 | 816 | 1,279.7 | 1,254.0 | 2,526.2 | +2.1% (+18.2 to +32.9) |

[^1]: OPR's error is inflated by rank deficiency early in each event, before there are enough matches to identify every
    team.

The pRidge-against-OPR intervals, which are not tabulated here, are all far from zero. EPA against OPR is within 1.1
percentage points of pRidge against OPR in every group.

### By round

pRidge against EPA, by the number of earlier matches the models learned from.

| Round | Regular rows | Regular gap | Championship rows | Championship gap |
| --- | --- | --- | --- | --- |
| 1 | 24,033 | +0.6% (-0.5 to +10.2) | 9,667 | +0.4% (-7.1 to +18.9) |
| 2 | 24,776 | +1.5% (+4.2 to +16.4) | 9,807 | +1.8% (+10.8 to +35.5) |
| 3 | 26,273 | +1.3% (+2.7 to +14.2) | 10,074 | +2.6% (+18.5 to +41.9) |
| 4 | 26,192 | +2.3% (+8.2 to +19.2) | 10,035 | +2.7% (+17.7 to +40.6) |
| 5 | 8,648 | +4.4% (+17.2 to +35.4) | 4,435 | +2.7% (+15.8 to +56.1) |
| 6 to 9 | none | none | 2,935 | +2.4% to +3.8%, intervals include zero from round 7 |

Regular-season round 6 has 2 events and no interval. Championship round 10 has 4 events and no interval.

#### Both tiers combined, rounds 2 to 5

All three models on the same rows, regular season and championship together.

| Round | Rows | Events | pRidge | EPA | OPR | pRidge vs EPA | pRidge vs OPR | EPA vs OPR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 34,583 | 4,248 | 869.9 | 856.0 | 2,460.6 | +1.6% | -64.6% | -65.2% |
| 3 | 36,347 | 4,243 | 799.6 | 785.0 | 1,128.6 | +1.9% | -29.1% | -30.4% |
| 4 | 36,227 | 4,236 | 755.3 | 737.3 | 902.1 | +2.4% | -16.3% | -18.3% |
| 5 | 13,083 | 2,187 | 882.4 | 852.7 | 990.7 | +3.5% | -10.9% | -13.9% |

OPR's error dramatically decreases once the matrix is no longer rank-deficient.

### By match index

pRidge against EPA at selected indices.

| Index | Regular rows | Regular gap | Championship rows | Championship gap |
| --- | --- | --- | --- | --- |
| 5 | 5,208 | -0.1% (-9.9 to +9.4) | 464 | +1.9% (-15.3 to +47.1) |
| 10 | 6,394 | -0.2% (-10.4 to +7.9) | 1,518 | +0.2% (-22.6 to +26.0) |
| 15 | 4,890 | +1.9% (+1.1 to +23.0) | 1,550 | +2.9% (+2.4 to +60.6) |
| 20 | 3,090 | +2.1% (-0.9 to +29.0) | 1,428 | -0.3% (-35.6 to +28.4) |
| 25 | 2,012 | +3.2% (+3.2 to +45.2) | 1,226 | +1.4% (-13.9 to +45.2) |
| 30 | 1,138 | +5.3% (+7.4 to +59.2) | 918 | +2.6% (-7.4 to +61.4) |

### 2025

| Group | Rows | Events | pRidge vs EPA |
| --- | --- | --- | --- |
| All rated events | 49,992 | 1,177 | +0.1% (-2.8 to +5.0) |
| Regular season | 34,762 | 939 | -0.3% (-5.8 to +2.4) |
| Championship | 15,230 | 238 | +0.7% (-2.4 to +16.8) |

By round in the 2025 regular season, pRidge against EPA reads -0.3%, +0.8%, -0.8%, -1.3% and +2.0% at rounds 1 to 5,
and every interval includes zero.

#### Regular season by round, all three models

| Round | Rows | Events | pRidge | EPA | OPR | pRidge vs EPA | pRidge vs OPR | EPA vs OPR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 6,951 | 927 | 588.9 | 590.9 | 1,212.4 | -0.3% (-8.2 to +5.0) | -51.4% | -51.3% |
| 2 | 7,136 | 938 | 511.5 | 507.5 | 1,320.2 | +0.8% (-3.9 to +12.4) | -61.3% | -61.6% |
| 3 | 7,535 | 937 | 453.2 | 456.8 | 623.6 | -0.8% (-10.5 to +3.0) | -27.3% | -26.8% |
| 4 | 7,507 | 935 | 427.7 | 433.2 | 498.1 | -1.3% (-12.3 to +1.3) | -14.1% | -13.0% |
| 5 | 2,514 | 481 | 455.2 | 446.3 | 497.5 | +2.0% (-1.3 to +18.9) | -8.5% | -10.3% |
