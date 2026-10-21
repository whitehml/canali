# Sim

Plays out an FTC event many times over to estimate each team's chances: where it ranks, whether it captains or is picked
for an alliance, whether its alliance wins, and whether it advances. It can also play out a region's whole season, event
by event, to estimate who reaches the championship and beyond. Each run starts from what the event has already
published, so matches already played count as played, and the results are saved in the warehouse.

## Quick start

```
uv sync --all-packages                                           # install everything in the .venv
uv run sim event 2025 EVENT_CODE --predictor epa-0.10.0          # simulate the rest of an event and print each team's chances
uv run sim event 2025 EVENT_CODE --predictor epa-0.10.0 --write  # the same, saved to the warehouse
uv run sim season 2025 --predictor epa-0.10.0 --rosters ROSTERS  # simulate a region's season, using its roster file
uv run pytest sim                                                # run the tests, no database needed
```

The warehouse must be running with the season's data loaded and EPA ratings computed for it, at the version named by
`--predictor`. The pick and award models must also have been measured on that version.

## Command surface

| Command | Does |
| --- | --- |
| `version` | Print the sim version |
| `event` | Simulate one event from its published progress, to convergence, and print each team's chances |
| `season` | Simulate a region's season from its roster file: its qualifiers in date order, then the championship |

`--predictor` is required and names the rating model and its version.

`event` takes `--through` to simulate from an earlier match, and `--teams` or `--rosters` to name the field of an event
with no published schedule. `season` takes `--before` to play the season from an earlier date. `--tolerance` sets the
standard error a run stops at, and `--seed` fixes the draws. Nothing persists without `--write`.

## Models

| Model | Fitted |
| --- | --- |
| Score model and RP curves | On the season's qualification rows before the event's week, at read time. Below `INIT_WINDOW` rows each stands in from earlier seasons |
| Alliance selection model | On every past season, one decision per pick. Stored per predictor version in `constants.py` and fixed through the season |
| Award models | On every past season, one per judged award. Stored per predictor version in `constants.py` and fixed through the season |

`measure_pick_model` in `pick_corpus.py` and `measure_award_models` in `award_corpus.py` re-measure the alliance selection and award
models. Their coefficients are in `constants.py`.

## Simulation execution

Repeated event trials reuse prepared alliance predictions, schedule relationships and award-history features.
Qualification tiebreakers are reused only after every qualification result is fixed. Award normalization and
alliance-seat lookups are prepared once per simulated field; candidate pools and random choices remain dynamic.
The native event runner retains this preparation for the run. Worker forecasts and browser comparisons rebuild it
at checkpoint-batch boundaries, so durable checkpoints contain only the existing counters and RNG state.

`EventCache` belongs to one run or batch and invalidates dependent values when the field, schedule or published
progress object changes. Inputs must remain read-only while cached; revisions and scenarios use new input objects.
There is no process-global event cache. This preparation does not change the convergence criterion, random draws
or probability models. See the [local performance measurements](../docs/forecast-benchmark-2026-10-21.md#python-simulation-optimization).

## Output

An event run lands in `derived.event_sim_run` and `derived.event_sim_result`, read through `pub.v_event_sim_run` and
`pub.v_event_sim_result`. A run is keyed by event, predictor, sim version and the match it was simulated from, and a
rerun at the same key replaces it.

| Column | Meaning |
| --- | --- |
| `p_attend` | Chance the team competes in that event |
| `mean_rank` | Average qualification rank when it attends |
| `rank_probabilities` | Chance of each qualification rank when it attends, first place first |
| `p_captain` | Chance it captains an alliance |
| `p_selected` | Chance a captain picks it |
| `p_win` | Chance its alliance wins the playoff |
| `p_advance` | Chance it is offered an advancement slot, null when the event offers none |
| `p_advance_first_championship` | Chance the slot is FIRST Championship's, null outside regional championships |

A season run lands in `derived.season_sim_run` and `derived.season_sim_result`, read through `pub.v_season_sim_run`
and `pub.v_season_sim_result`. A run is keyed by season, region, predictor, sim version and the count of the region's
events started, and a rerun at the same key replaces it.

| Column | Meaning |
| --- | --- |
| `p_attend_rcmp` | Chance the team plays the regional championship |
| `p_premier` | Chance the championship sends it to a Premier event |
| `p_cmp` | Chance the championship sends it to FIRST Championship |

## Layout

```
src/sim/
  schedule.py           the qualification schedule, published or generated
  score.py              an alliance's score sampled around its prediction
  rp_source.py          where each bonus RP's quantity comes from
  rp_probability.py     bonus RP probability
  match.py              a match's scores, result and bonus RPs sampled together
  fit.py                the score model and RP curves on a weekly cutoff
  predictor.py          the rating model the fits and draws read
  standings.py          ranking points, ranking score and tiebreakers
  conditional_logit.py  one option chosen among several by weighted traits
  pick_model.py         the alliance selection model
  pick_corpus.py        the alliance selection fitting data and its measurement
  selection.py          alliance selection
  playoff.py            the double-elimination playoff
  award_model.py        the award models
  award_corpus.py       the award fitting data and its measurement
  awards.py             judged awards after selection
  advancement.py        advancement points and slots
  event.py              one event played through
  progress.py           what an event has published so far
  inputs.py             the field, slots and models an event reads
  convergence.py        when a run has converged
  runner.py             the Monte Carlo over an event
  pipeline.py           one event from its published state
  season.py             a region's season played through
  rosters.py            roster files for events with no schedule
  writer.py             runs written to the warehouse
  constants.py          the fitted and measured constants
  cli.py                the sim command
docs/                   methodology for each model
tests/                  unit tests
```

## Versions

The [local Python Worker](../worker/README.md) can run the event simulator from a prepared snapshot without Postgres.
It checkpoints `EventAccumulator` counts and the random-generator state between bounded batches, preserving the
native runner's convergence criterion and summary.

`__version__` in `__init__.py` is the sim version stored on every run. Bump it whenever a run made now would not be
comparable to one already stored under that version. The predictor's version is stored beside it.

## See also

- [`docs/score-distribution.md`](docs/score-distribution.md): the probability distribution observed on score outcomes from predictions.
- [`docs/score-and-rp-model.md`](docs/score-and-rp-model.md): how match outcomes and RP are modeled.
- [`docs/fouls.md`](docs/fouls.md): how fouls affect the models.
- [`docs/pick-model.md`](docs/pick-model.md): alliance selection model.
- [`docs/award-model.md`](docs/award-model.md): judged award model.
- [`docs/rosters.md`](docs/rosters.md): event registration input format.
- [`../docs/open-questions.md`](../docs/open-questions.md#sim): pending investigations.
