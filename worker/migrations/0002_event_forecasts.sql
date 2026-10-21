CREATE TABLE forecast_input (
    event_code TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision > 0),
    document TEXT NOT NULL CHECK (json_valid(document))
);

CREATE TABLE forecast_state (
    event_code TEXT PRIMARY KEY REFERENCES forecast_input(event_code),
    input_revision INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    checkpoint TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(checkpoint)),
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'complete', 'exhausted')),
    result TEXT CHECK (result IS NULL OR json_valid(result))
);
