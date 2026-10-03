# Navigating the warehouse with psql

psql is a way to inspect the database directly, base tables included.

| | |
| --- | --- |
| Database | `warehouse` |
| Schemas | `pub`, `core`, `derived`, `raw` |
| Seasons | 2022+ |
| Views | The static files under `views/` and their derivatives, plus the generated per-season views |
| Writer | the database owner, alone |

## Get a shell

The local server is `pgserver`, bundled binaries, no root. It listens on a Unix socket only, under
`var/pgdata` at the repository root, whatever the working directory.

```
cd canali
uv run warehouse db start        # boot it; prints a URL
# psql is not on PATH; it ships inside the workspace venv:
PSQL=.venv/lib/python3.12/site-packages/pgserver/pginstall/bin/psql
$PSQL "postgresql://postgres@/warehouse?host=$PWD/var/pgdata"
```

Stop it with `uv run warehouse db stop`.

`warehouse config` prints what the process resolved: the database URL, the payload root, and whether credentials
are set.

## Set db search path

Bare `\dt` and `\dv` only look at your `search_path`, `("$user", public)`, where nothing but `alembic_version`
lives. Every real object is in `pub`, `core`, `derived` or `raw`.

```sql
-- this session
SET search_path TO pub, core, derived, raw, public;
-- permanently, for this database; applies on next connect
ALTER DATABASE warehouse SET search_path TO pub, core, derived, raw, public;
```

Then `\dt`, `\dv`, `\d award` and unqualified `SELECT`s all resolve.

Row-count estimates read 0 until statistics are gathered. Run `ANALYZE;` once. `count(*)` is always exact.

## Schema map

| Schema | What is in it |
| --- | --- |
| `pub` | The contract. Reference data, matches and scoring, standings and awards, model output, and ingest health |
| `core` | What FIRST publishes: `season`, `event`, `team`, `team_season`, `event_team`, `match`, `match_team`, `match_breakdown`, `ranking`, `award`, `playoff_alliance`, `playoff_alliance_pick`, `advancement_points`, `advancement_slot`, `event_advancement`, `rule_pack*`[^rule-packs] |
| `derived` | What the warehouse computes: `team_event_opr`, `fit_run`, `team_epa`, `team_pridge`. Empty until `derive opr` and the model pipelines have run |
| `raw` | Ingest bookkeeping: `ingest_run`, `raw_payload`, `ingest_conflict`, `ingest_diff`, `match_signal` |
| `public` | Just `alembic_version` |

List them with `\dn`, tables in one with `\dt core.*`, full detail with `\d+ core.match`, and a table's own comment
with `\dt+ core.*`.

[^rule-packs]: A rule pack is one season's scoring vocabulary, a TOML file under `rule_packs/` named for the season
    and the game, loaded into `core.rule_pack` and `core.rule_pack_component` by `warehouse rules load`. Its
    component rows name every field a breakdown can carry, its type, and whether it is a subtotal, derived, or
    recovered from other fields. A finest-grain leaf also carries `partition_group = "leaf"` and a `phase` of auto or
    teleop. `state_scoring` is for enum fields that FIRST does not report numerical scoring for.

## Contract views

| View | Grain and note |
| --- | --- |
| `v_event`, `v_event_size`, `v_event_sequence` | One event. Size is how many teams FTC Events lists for it, known once the schedule posts. Sequence is the event order both models consume |
| `v_team`, `v_team_season` | `v_team` is the team number and nothing that changes. `v_team_season` is what the team called itself that year: name, region, city |
| `v_match`, `v_match_team` | One match, and one row per station: four per match, two per alliance. Carries surrogate, no-show and dq |
| `v_match_rating_input` | Two rows per scored match, one per alliance: its score, its opponent's, and fouls split into committed and received |
| `v_breakdown_2022` … `v_breakdown_2025` | Typed per-season scoring components, one row per match and alliance |
| `v_phase_points_2022` … `v_phase_points_2025` | One row per match and alliance: `auto_sum`, `teleop_sum` and `total_sum`, each summed from the season's finest-grain leaves |
| `v_match_unratable` | Matches whose breakdown does not add up to the official non-foul score. Models treat them as unplayed.|
| `v_ranking`, `v_match_level_order` | Published standings with their tiebreak columns, and the level sort key |
| `v_award` | One row per award slot awarded |
| `v_alliance_selection`, `v_alliance_pick` | Seeded alliances and the ordered pick log |
| `v_opr` | The computed OPR baseline: auto, teleop, total |
| `v_fit_run`, `v_team_epa`, `v_team_epa_pre_event`, `v_team_pridge` | Model output. `v_fit_run` publishes completed batch runs only. Join it at a named version |
| `v_rule_pack`, `v_rule_pack_component` | A season's scoring vocabulary. |
| `v_ingest_cursor`, `v_ingest_conflict`, `v_ingest_diff` | What ingest fetched and when, where two suppliers disagreed, and what an in-season re-pull overwrote |

## Reading award rows

`v_award` holds one row per award slot awarded. No name is stored, so `award_code` is the award and `series` is the
placement, 1 being first place.

| Code | Award | | Code | Award |
| --- | --- | --- | --- | --- |
| 1 | Judges Choice | | 8 | Connect |
| 3 | Promote | | 9 | Think |
| 4 | Control | | 11 | Inspire |
| 5 | Motivate | | 25 | Reach |
| 6 | Design | | 26 | Sustain |
| 7 | Innovate | | | |

### Example: one team's award history

Every award slot a team has been named on, in time order.

```sql
SELECT e.season, e.code AS event, e.date_start, a.award_code, a.series
FROM   pub.v_award a
JOIN   core.event e USING (event_id)
WHERE  a.team_number = 13474
ORDER BY e.date_start, a.award_code;
```

## Raw data

Ingested data lives in four places. The `raw` tables fill whenever `warehouse ingest` runs against this cluster.
The payload bytes sit on disk regardless.

### Ingest bookkeeping

| Table and view | Holds |
| --- | --- |
| `raw.ingest_run`, `pub.v_ingest_cursor` | One row per scope and endpoint: the conditional-request cursor. `last_modified`, `payload_hash`, `last_status`, `last_checked_at_utc`, `last_changed_at_utc`, and the run, check and empty counts |
| `raw.raw_payload` | The payload index: hash to path, endpoint, `fetched_at_utc`, `last_modified`, byte length. Maps a file on disk back to the call that produced it |
| `raw.ingest_conflict`, `pub.v_ingest_conflict` | Where two suppliers disagreed on one key: `table_name`, `key`, `kind`, `stored`, `incoming`, `source` |
| `raw.ingest_diff`, `pub.v_ingest_diff` | Active-season revisions: `before` and `after` for a row an in-season re-pull overwrote |

```sql
-- freshest and most-checked endpoints
SELECT endpoint, scope, last_status, run_count, empty_count, last_changed_at_utc
FROM pub.v_ingest_cursor ORDER BY last_checked_at_utc DESC;

-- every stored payload for one event's scores
SELECT endpoint, byte_length, fetched_at_utc, path
FROM raw.raw_payload WHERE endpoint LIKE '%/scores/%';
```

### Row-level provenance

Some rows carry when they landed, and awards carry which supplier sent them.

- `core.match.ingested_at_utc`
- `core.playoff_alliance.ingested_at_utc`, `core.playoff_alliance_pick.ingested_at_utc`
- `core.event_team.first_observed_at_utc`
- `core.award.source`, `ftc_events` or `ftcscout`

No `core` row points at a payload hash. The route to the bytes is `raw.ingest_run.payload_hash`, joined to
`raw.raw_payload`.

### Raw match scoring JSON

`core.match_breakdown.breakdown` is `jsonb`, the per-alliance score object exactly as the API delivered it, one row
per match and alliance. `pub.v_breakdown_<season>` flattens it into typed columns.

```sql
-- see the raw object
SELECT alliance, jsonb_pretty(breakdown)
FROM core.match_breakdown WHERE match_id = '…';

-- what keys exist for a season
SELECT DISTINCT jsonb_object_keys(b.breakdown) AS k
FROM core.match_breakdown b
JOIN core.match m USING (match_id)
JOIN core.event e USING (event_id)
WHERE e.season = 2025 ORDER BY k;

-- pull a field out; ->> returns text, cast as needed
SELECT (breakdown->>'autoPoints')::int, (breakdown->>'totalPoints')::int
FROM core.match_breakdown LIMIT 5;

-- or read it already typed
SELECT alliance, auto_points, teleop_points, total_points
FROM pub.v_breakdown_2025 WHERE match_id = '…';
```

### On-disk payload archive

`var/payloads/` at the repository root holds gzipped raw HTTP bodies, content-addressed and sharded
`<xx>/<yy>/<sha256>.json.gz`. Not SQL, so inspect it from the shell.

```
H=8e3bb947502b84115d60911c319609513fdf626a…
zcat var/payloads/${H:0:2}/${H:2:2}/$H.json.gz | jq '.[0]'
# which call was this?
$PSQL "$URL" -c "select endpoint, fetched_at_utc from raw.raw_payload where payload_hash = '$H'"
```

## Meta-commands

| Command | Does |
| --- | --- |
| `\d name`, `\d+ name` | Columns, types, indexes, view SQL |
| `\dt s.*`, `\dv`, `\di` | Tables, views, indexes |
| `\dn`, `\l` | Schemas, databases |
| `\sv v_award` | Show a view's definition |
| `\x auto` | Expanded rows for wide results |
| `\pset null '∅'` | Make NULLs visible |
| `\timing on` | Time every query |
| `\copy (SELECT …) TO 'out.csv' CSV HEADER` | Export client-side |
| `\g out.txt`, `\gx` | Send to file, run expanded |
| `\watch 5` | Re-run every 5 s |
| `\e`, `\i file.sql` | Edit in `$EDITOR`, run a script |
| `\gexec` | Run each row of the result as SQL |

Non-interactive: `$PSQL "$URL" -c "…"`, `-f file.sql`, and `-A -t -F$'\t'` for tab-separated scriptable output.

## Quirks

- **TCP against socket.** The CLI reads `WAREHOUSE_DATABASE_URL`, which defaults to TCP on localhost, but
  `pgserver` listens on the socket only. Put the socket URL in `warehouse/.env` or `db upgrade` and `ingest` fail
  with connection refused. Strip `+psycopg` before handing a SQLAlchemy URL to psql.
- **`v_award` is judged team awards only.** Unawarded slots, the person-held awards and the alliance-outcome awards
  are dropped at ingest, so `award_code` is always one of the 11 above.
- **Replayed matches leave no trace.** A replayed result overwrites the superseded one and no history is kept.
- **Row estimates lie until `ANALYZE`.** `\dt+` sizes and planner estimates read 0 on a fresh cluster. Trust
  `count(*)`.

## See also

- `data-sources.md`: what each supplier provides, and its known quirks.
- `../README.md`: schemas, roles and the command surface.
