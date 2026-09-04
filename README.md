# Canali

An FTC statistics stack: raw data collected from external APIs, all derived statistics computed in-house.

| Module | Description |
| --- | --- |
| `warehouse/` | API ingest, storage, and eventual provider of in-house statistics |
| `epa/` | Event-sequential expected points added. An Elo-style model based on Statbotics' methodology for FRC |
| `pridge/` | Ridge regression regularized toward pre-event EPA, based on work by Gabriel Krotkov and FRC 449 |
| `analysis/` | Evaluation scripts and reproducible samples |
