-- Operational surface: what ingest has fetched, what the suppliers disagreed about,
-- and what an upstream change overwrote.

CREATE VIEW pub.v_ingest_cursor AS
SELECT r.scope, r.season, r.event_id, e.code AS event_code, r.endpoint,
       r.last_modified, r.payload_hash, r.last_checked_at_utc, r.last_changed_at_utc,
       r.last_status, r.run_count, r.check_count, r.empty_count, r.rows_written
FROM raw.ingest_run r
LEFT JOIN core.event e ON e.event_id = r.event_id;

CREATE VIEW pub.v_ingest_conflict AS
SELECT observed_at_utc, table_name, key, kind, stored, incoming, source, payload_hash
FROM raw.ingest_conflict;

CREATE VIEW pub.v_ingest_diff AS
SELECT observed_at_utc, table_name, key, before, after, payload_hash
FROM raw.ingest_diff;
