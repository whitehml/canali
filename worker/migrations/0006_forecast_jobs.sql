-- The current forecast_input is the desired revision; this row owns the active work.
CREATE TABLE forecast_job (
    event_code TEXT PRIMARY KEY REFERENCES forecast_input(event_code),
    input_revision INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    instance_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'running', 'yielded', 'complete', 'exhausted', 'superseded', 'errored')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
