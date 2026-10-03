# Canali

An FTC robotics statistics stack: raw data collected from external APIs; all derived statistics computed in-house.

| Module | Description |
| --- | --- |
| [`warehouse/`](warehouse/README.md) | API ingest, storage, and provider of in-house statistics |
| [`epa/`](epa/README.md) | Event-sequential expected points added. An Elo-style model based on [Statbotics](https://www.statbotics.io/)' methodology for FRC |
| [`pridge/`](pridge/README.md) | Ridge regression regularized toward pre-event EPA, based on work by [Gabriel Krotkov and FRC 449](https://www.chiefdelphi.com/t/introducing-prior-ridge-regularization-for-frc-rating/519531) |
| [`analysis/`](analysis/README.md) | Head-to-head comparison of pRidge, EPA, and OPR on the same alliances |

The models read the warehouse and write ratings back to it. `analysis` reads matches and EPA ratings from the warehouse and refits pRidge and OPR itself.

## Quick start

```bash
uv sync --all-packages     # .venv, every workspace member, the dev group
uv run pytest              # tests that need no database
```

Each module's README has its own quick start. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the development environment, test tags, and style.

## Documentation

| Document | Covers |
| --- | --- |
| [`warehouse/docs/data-sources.md`](warehouse/docs/data-sources.md) | What each supplier provides and its known quirks |
| [`warehouse/docs/psql-guide.md`](warehouse/docs/psql-guide.md) | Local database setup and navigation |
| [`warehouse/docs/open-questions.md`](warehouse/docs/open-questions.md) | Pending investigations |
| [`epa/docs/methodology.md`](epa/docs/methodology.md) | The EPA model, its constants, and the evidence behind them |
| [`epa/docs/open-questions.md`](epa/docs/open-questions.md) | Pending investigations |
| [`pridge/docs/methodology.md`](pridge/docs/methodology.md) | The pRidge model, its constants, and the evidence behind them |
| [`pridge/docs/loocv-derivation.md`](pridge/docs/loocv-derivation.md) | Leave-one-out error from a single fit |
| [`analysis/docs/head-to-head.md`](analysis/docs/head-to-head.md) | The comparison method and the standing result |
