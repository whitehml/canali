# pRidge

Per-event ridge regression for FTC, regularized toward pre-event EPA. Reads matches and pre-event EPA from the warehouse
and writes ratings to `derived.team_pridge`.

## Quick start

```
uv sync --all-packages                                  # .venv, every workspace member, the dev group
uv run pridge version                                   # the model version ratings are stored under
uv run pridge fit-event 2025 EVENT_CODE --prior-version epa-0.10.0   # fit one event and print it; --write refits every match and persists
uv run pridge backfit 2025 --prior-version epa-0.10.0    # fit every rated event of a season; --write persists it
uv run pridge evaluate --prior-version epa-0.10.0        # next-match error by tier and match index
uv run pytest pridge                                    # unit tests, no database needed
```

The commands read `pub.v_match_rating_input` and `pub.v_team_epa_pre_event`, so the warehouse must be running and
ingested, and an EPA batch run at the named version must exist for each season.

## Command surface

| Command | Does |
| --- | --- |
| `version` | Print the model version |
| `fit-event` | Fit one event and print the result, or with `--write` refit it at every match index and persist |
| `backfit` | Fit every rated event of a season once, at its last match |
| `evaluate` | Next-match MSE over a sample of events, pooled, by tier and by match index |
| `derive-lambda` | The lambda minimizing leave-one-out error for one constant's events, pooled over seasons |
| `season-lambda` | Whether a season wants a lambda other than the shipped constant |

`--prior-version` is required wherever a prior is read, and names the EPA model version.
Nothing persists without `--write`, which `backfit` and `fit-event` accept. The others print.

`--seasons` takes a comma-separated list. `--limit` samples that many events per season.

## Output

Ratings land in `derived.team_pridge` under one `derived.fit_run`, and are read through `pub.v_team_pridge`.

| Column | Meaning |
| --- | --- |
| `as_of_match` | The match index the fit was taken at |
| `component` | `total`, or one scoring component of the rule pack's partition |
| `pridge` | The team's rating, in season points |
| `lambda_` | The penalty the fit used |

The component ratings sum to the total rating.

A fit run records the EPA version its prior came from as `prior_version`. A completed batch run, from `backfit`,
replaces the run with the same model, season, `model_version` and prior version. A live event run, from
`fit-event --write`, is kept per event. `ops fit-runs` and `ops drop-model-version` in the warehouse inspect and retire
them.

## Layout

```
src/pridge/
  design.py      the design matrix and response for one event
  estimator.py   the pRidge fit and leave-one-out residuals
  prior.py       the prior and where it is read from
  fit.py         one event fit, at the tier's lambda or at a per-fit lambda
  evaluate.py    next-match prediction and error by match index
  tune.py        lambda re-derivation by pooled leave-one-out error
  constants.py   the versioned constants
  pipeline.py    load, fit, persist
  cli.py         the pridge command
  testing/       synthetic events for tests
docs/          methodology and the leave-one-out derivation
tests/         unit tests over synthetic events
```

## Versions

`MODEL_VERSION` in `constants.py` names a set of ratings that may be compared with one another. Bump it whenever a
rating computed now would not be comparable to one already stored under that name, including a change to the model
outside of this file. A change of prior source does not bump it, since every fit run records which prior it used.

`derive-lambda` and `season-lambda` print a recommendation. Constants are edited by hand and the version bumped.

## See also

- [`docs/methodology.md`](docs/methodology.md): the model, its constants, and the evidence behind them.
- [`docs/loocv-derivation.md`](docs/loocv-derivation.md): why leave-one-out error comes from one fit.
