# Analysis

Compares pRidge, EPA and OPR on the same alliances, by match index and by round. Reads matches and EPA ratings from the
warehouse and writes a Markdown report.

## Quick start

```
uv sync --all-packages                                  # .venv, every workspace member, the dev group
uv run analysis --epa-version epa-0.9.0 --pridge-version pridge-0.4.0            # print the report
uv run analysis --epa-version epa-0.9.0 --pridge-version pridge-0.4.0 --out report.md
uv run analysis --epa-version epa-0.9.0 --pridge-version pridge-0.4.0 --identified-only
```

The command reads `pub.v_match_rating_input`, `pub.v_fit_run`, `pub.v_team_epa` and `pub.v_team_epa_pre_event`, so the
warehouse must be running and ingested, and an EPA batch run at the named version must exist.

## Command

`analysis` is one command.

| Option | Does |
| --- | --- |
| `--epa-version` | The EPA model version. Required. It is also the prior pRidge regularizes toward |
| `--pridge-version` | The pRidge version. Required, and must be the installed engine |
| `--seasons` | Comma-separated, default 2022 through 2025 |
| `--identified-only` | Score OPR only at indices where its design has full column rank, the paper's start |
| `--out` | Write the report to a file instead of printing it |

A season with no completed EPA batch run at `--epa-version` is skipped with a warning. With no such season the command
fails.

## Models

Every model predicts the alliances of each qualification match after the first, from the ratings held at the match
before it.

| Model | Ratings at match k |
| --- | --- |
| EPA | Read from the version's batch run: each team's latest match-grain row at or before k, else its pre-event row |
| pRidge | Refit on the matches up to k toward the EPA version's pre-event ratings, at the tier's lambda |
| OPR | Least squares on the matches up to k, refit at every index |

pRidge and OPR are refit on every run, so the `--pridge-version` check is against the installed engine, and nothing is
read from `derived.team_pridge` or `derived.team_event_opr`.

A match is scored only if both alliances are fully rated by every model. The report states how many rows each model
lost to reach the common set.

## Output

The report opens with the versions and runs that produced it: the EPA version with its run and finish time in each
season, and the pRidge version with the prior it read. Six tables follow, three model pairs each by match index and by
round.

| Column | Meaning |
| --- | --- |
| rows | Alliance rows in the bucket |
| events | Events those rows come from |
| MSE | Mean squared error of each model over the bucket |
| difference | The first model's MSE minus the second's |
| 95% interval | Paired bootstrap over events, withheld below twelve events |

A round is the number of earlier matches every rated team on the alliance has played, counting surrogate appearances and
not no-shows. An alliance whose teams disagree has no round and is left out of the by-round tables.

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
