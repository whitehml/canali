# Canali

An FTC robotics statistics stack: raw data collected from external APIs; all derived statistics computed in-house.

| Module | Description |
| --- | --- |
| [`warehouse/`](warehouse/README.md) | API ingest, storage, and provider of in-house statistics |
| [`epa/`](epa/README.md) | Event-sequential expected points added. An Elo-style model based on [Statbotics](https://www.statbotics.io/)' methodology for FRC |
| [`pridge/`](pridge/README.md) | Ridge regression regularized toward pre-event EPA, based on work by [Gabriel Krotkov and FRC 449](https://www.chiefdelphi.com/t/introducing-prior-ridge-regularization-for-frc-rating/519531) |
| [`sim/`](sim/README.md) | Monte Carlo simulation of an event and of a region's season: standings, alliance selection, the playoff, awards and advancement |
| [`live/`](live/README.md) | The automated polling of a live event/season |
| [`analysis/`](analysis/README.md) | Head-to-head comparison of pRidge, EPA, and OPR on the same alliances |
| [`worker/`](worker/README.md) | Local Python Workers + D1: FIRST imports, resumable season EPA, match forecasts and event simulations |
| [`frontend/`](frontend/README.md) | Vite + Svelte season browser, published D1 forecasts and custom Python WebAssembly simulations on the visitor's device |

The models read the warehouse and write ratings back to it. `analysis` reads matches and EPA ratings from the warehouse and refits pRidge and OPR itself.

## Quick start

```bash
uv sync --all-packages     # install everything in the .venv
uv run pytest              # run the tests, no database needed
```

Each module's README has its own quick start. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the development environment, test tags, and style.

## Documentation

| Document | Covers |
| --- | --- |
| [`docs/open-questions.md`](docs/open-questions.md) | Pending investigations, by module |
| [`warehouse/docs/data-sources.md`](warehouse/docs/data-sources.md) | What each supplier provides and its known quirks |
| [`warehouse/docs/psql-guide.md`](warehouse/docs/psql-guide.md) | Local database setup and navigation |
| [`epa/docs/methodology.md`](epa/docs/methodology.md) | The EPA model, its constants, and the evidence behind them |
| [`pridge/docs/methodology.md`](pridge/docs/methodology.md) | The pRidge model, its constants, and the evidence behind them |
| [`pridge/docs/loocv-derivation.md`](pridge/docs/loocv-derivation.md) | Leave-one-out error from a single fit |
| [`sim/docs/score-distribution.md`](sim/docs/score-distribution.md) | The probability distribution observed on score outcomes from predictions |
| [`sim/docs/score-and-rp-model.md`](sim/docs/score-and-rp-model.md) | How match outcomes and RP are modeled. |
| [`sim/docs/fouls.md`](sim/docs/fouls.md) | How fouls affect the models |
| [`sim/docs/pick-model.md`](sim/docs/pick-model.md) | Alliance selection model |
| [`sim/docs/award-model.md`](sim/docs/award-model.md) | Judged award model |
| [`sim/docs/rosters.md`](sim/docs/rosters.md) | Event registration input format |
| [`analysis/docs/head-to-head.md`](analysis/docs/head-to-head.md) | The comparison method and the standing result |

## Why Canali?

I volunteer with [Mars Robotics](https://www.marsroboticsassociation.org/) in Mars, PA.

The name comes from the illusory [Martian canals](https://en.wikipedia.org/wiki/Martian_canals). *Canali* is Italian
for channels. Astronomers looking through the telescopes of the day reported canali crossing the surface of Mars, the
word was misconstrued in English as canals, and some took them for waterways built by Martians. The lines were never
there, a pattern drawn from faint and imperfect observations.

The business of statistics is finding patterns in noisy data, the name "canali" is an on-brand warning that convincing
patterns can still be wrong. It's also a reminder that the goal is not to squint harder, but to
build better instrumentation.

## License

[MIT](LICENSE).
