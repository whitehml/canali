CREATE TABLE season_run (
    season INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL CHECK(json_valid(config)),
    engine_version TEXT NOT NULL,
    cursor TEXT NOT NULL DEFAULT '',
    init_scale TEXT,
    scale_dirty INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE season_event (
    season INTEGER NOT NULL REFERENCES season_run(season),
    event_code TEXT NOT NULL,
    event_key TEXT NOT NULL,
    document TEXT NOT NULL CHECK(json_valid(document)),
    source TEXT NOT NULL CHECK(json_valid(source)),
    PRIMARY KEY(season, event_code),
    UNIQUE(season, event_key)
);
CREATE TABLE season_rating (
    season INTEGER NOT NULL REFERENCES season_run(season),
    event_key TEXT NOT NULL,
    team INTEGER NOT NULL,
    epa REAL NOT NULL,
    played INTEGER NOT NULL,
    last_played TEXT NOT NULL,
    PRIMARY KEY(season, event_key, team)
);
CREATE INDEX season_team_history ON season_rating(season, team, event_key DESC);

CREATE TRIGGER season_config_changed AFTER UPDATE OF config, engine_version ON season_run
WHEN OLD.config != NEW.config OR OLD.engine_version != NEW.engine_version
BEGIN
    DELETE FROM season_rating WHERE season = NEW.season;
    UPDATE season_run SET revision = revision + 1, cursor = '', init_scale = NULL, scale_dirty = 1
    WHERE season = NEW.season;
END;
CREATE TRIGGER season_scale_changed AFTER UPDATE OF init_scale ON season_run
WHEN OLD.init_scale IS NOT NEW.init_scale
BEGIN
    DELETE FROM season_rating WHERE season = NEW.season;
END;
CREATE TRIGGER season_event_added AFTER INSERT ON season_event
BEGIN
    DELETE FROM season_rating WHERE season = NEW.season AND event_key >= NEW.event_key;
    UPDATE season_run SET revision = revision + 1, scale_dirty = 1,
        cursor = CASE WHEN cursor >= NEW.event_key THEN
            coalesce((SELECT max(event_key) FROM season_event
                      WHERE season = NEW.season AND event_key < NEW.event_key), '') ELSE cursor END
    WHERE season = NEW.season;
END;
CREATE TRIGGER season_event_changed AFTER UPDATE ON season_event
WHEN OLD.document != NEW.document OR OLD.source != NEW.source OR OLD.event_key != NEW.event_key
BEGIN
    DELETE FROM season_rating WHERE season = NEW.season AND event_key >= min(OLD.event_key, NEW.event_key);
    UPDATE season_run SET revision = revision + 1, scale_dirty = 1,
        cursor = CASE WHEN cursor >= min(OLD.event_key, NEW.event_key) THEN
            coalesce((SELECT max(event_key) FROM season_event
                      WHERE season = NEW.season AND event_key < min(OLD.event_key, NEW.event_key)), '') ELSE cursor END
    WHERE season = NEW.season;
END;
