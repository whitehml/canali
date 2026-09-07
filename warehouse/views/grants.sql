-- Applied last in the rebuild, and re-applied after every migration, so ON ALL TABLES is re-evaluated once new tables
-- exist.

REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- NOLOGIN. A person gets a login role of their own and GRANT ftc_readonly TO it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ftc_readonly') THEN
        CREATE ROLE ftc_readonly NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA pub TO ftc_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA pub TO ftc_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA pub GRANT SELECT ON TABLES TO ftc_readonly;

-- The owner is the only writer. Ingest, the OPR baseline, both model pipelines and ops all connect as it.
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA raw FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA derived FROM PUBLIC;
