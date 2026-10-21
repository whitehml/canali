-- Prepared qualification inputs, independent of the PostgreSQL warehouse.
CREATE TABLE event_input (
    event_code TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision > 0),
    document TEXT NOT NULL CHECK (json_valid(document))
);

CREATE TABLE event_result (
    event_code TEXT PRIMARY KEY REFERENCES event_input(event_code),
    input_revision INTEGER NOT NULL,
    document TEXT NOT NULL CHECK (json_valid(document)),
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
