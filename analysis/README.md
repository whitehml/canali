# Analysis

Compares pRidge, EPA and OPR on the same alliances, by match index and by round. Reads matches and EPA ratings from the
warehouse and writes a Markdown report.

## Quick start

```
uv sync --all-packages                                                            # .venv, every workspace member, the dev group
uv run analysis --epa-version epa-0.10.0 --pridge-version pridge-0.5.0            # print the report
uv run analysis --epa-version epa-0.10.0 --pridge-version pridge-0.5.0 --out report.md
uv run analysis --epa-version epa-0.10.0 --pridge-version pridge-0.5.0 --identified-only
```

The command reads `pub.v_match_rating_input`, `pub.v_fit_run`, `pub.v_team_epa` and `pub.v_team_epa_pre_event`, so the
warehouse must be active, and model runs at the named versions must exist.

## Command

`analysis` is one command.

| Option | Does |
| --- | --- |
| `--epa-version` | The EPA model version. Required. It is also the prior pRidge regularizes toward |
| `--pridge-version` | The pRidge version. Required, and must be the installed engine |
| `--seasons` | Comma-separated, default 2022 through 2025 |
| `--identified-only` | Score OPR only at indices where its design has full column rank, the paper's start |
| `--out` | Write the report to a file instead of printing it |

A season with no completed EPA batch run at `--epa-version` is skipped with a warning.

## Models

Every model predicts the alliances of each qualification match after the first, from the ratings held at the match
before it.

| Model | Ratings at match k |
| --- | --- |
| EPA | Read from the version's batch run: each team's latest match-grain row at or before k, else its pre-event row |
| pRidge | Refit on the matches up to k toward the EPA version's pre-event ratings, at the tier's lambda |
| OPR | Least squares on the matches up to k, refit at every index |

pRidge and OPR are refit on every run.

A match is scored only if both alliances are fully rated by every model. The report states how many rows each model
lost to reach the common set.

## Output

The report contains tables for the three model pairs, pooled over all events and by round, then for each tier group
(regular season, championships) pooled, by match index and by round.

| Column | Meaning |
| --- | --- |
| rows | Alliance rows in the bucket |
| events | Events those rows come from |
| MSE | Mean squared error of each model over the bucket |
| difference | The first model's MSE minus the second's |
| 95% interval | Paired bootstrap over events, withheld below `MIN_EVENTS_FOR_INTERVAL` events in `report.py` |

Round n is a match where every rated team on the alliance is playing its nth match as the models see it, counting
surrogate appearances but not no-shows.

## Layout

```
src/analysis/
  comparison.py  shared rows, the paired bootstrap, by match and by round
  forecasts.py   the three models as forecasts over alliance rows
  rounds.py      round assignment
  report.py      provenance and the Markdown report
  cli.py         the analysis command
```

## See also

- [`docs/head-to-head.md`](docs/head-to-head.md): the method and the standing result.
