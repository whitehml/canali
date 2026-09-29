# EPA

Event-sequential Expected Points Added for FTC, fitted per season from the rule pack. Reads matches from the warehouse
and writes ratings to `derived.team_epa`.

## Quick start

```
uv sync --all-packages                        # .venv, every workspace member, the dev group
uv run epa check                              # reconcile each season's partition, fit nothing
uv run epa replay --write                     # replay 2022-2025 and persist the ratings
uv run epa evaluate                           # next-match error and the per-tier cross-section
uv run pytest epa                             # unit tests, no database needed
```

The commands read `pub.v_match_rating_input`, so the warehouse must be running and ingested first.

## Command surface

| Command | Does |
| --- | --- |
| `check` | Verify each season's component partition against the no-foul total |
| `replay` | Replay seasons in order, carrying ratings across the transitions |
| `update` | Rate one in-progress event, continuing a named version's batch run |
| `evaluate` | Next-match MSE and MAE, the early-event split, and the per-tier cross-section |
| `fit-constants` | Search K and M, and fit carryover; prints a proposal |
| `fit-layoff` | Search the layoff boost against next-match error |
| `constants` | The constants a season runs on |

`replay` and `update` persist only with `--write`.

`--seasons` takes a comma-separated list and replays in the order given.

## Output

Ratings land in `derived.team_epa` under one `derived.fit_run`, and are read through `pub.v_team_epa`.

| Tag | Row |
| --- | --- |
| `season_start` | A team's rating entering the season, with no event |
| `pre_event` | A team's rating at its first match of an event, layoff boost applied |
| `match` | A team's rating after one of its matches |
| `post_event` | A team's rating after its last match of an event |

Each row carries `epa_scaled`, in season points, `epa_norm`, the cross-season percentile rating, and `components`, the
per-component series that sum to the total.

A completed batch run replaces the run with the same model, season, `model_version` and prior version. A live event
run, from `update`, is kept per event. `ops fit-runs` and `ops drop-model-version` in the warehouse inspect and retire
them.

## Layout

```
src/epa/
  model.py       the update rule and its K and M schedules
  partition.py   the season's components, the reconciliation assertion, and each row's response
  corpus.py      the event-sequential match stream
  replay.py      the season replay loop and the four rating tags
  scale.py       season and init scales, carryover, rookie seeding, layoff boost
  norm.py        the cross-season percentile rating
  evaluate.py    next-match prediction, error metrics, tier cross-section
  fit.py         constant searches with leave-one-season-out selection
  constants.py   the versioned constants
  pipeline.py    load, assert, replay, persist
  cli.py         the epa command
docs/          methodology
tests/         unit tests over synthetic streams
```

## Versions

`MODEL_VERSION` in `constants.py` names a set of ratings that may be compared with one another. Bump it whenever a
rating computed now would not be comparable to one already stored under that name, including a change to the model outside of this file.

`fit-constants` and `fit-layoff` print a proposal. Constants are edited by hand and the version bumped.

## See also

- [`docs/methodology.md`](docs/methodology.md): the model, its constants, and the rationale behind them.
