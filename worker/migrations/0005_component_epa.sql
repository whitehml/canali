ALTER TABLE season_event ADD COLUMN partition_version TEXT NOT NULL DEFAULT '';
CREATE INDEX season_partition_version ON season_event(season, partition_version);
ALTER TABLE season_rating ADD COLUMN components TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(components));
