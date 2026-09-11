# Data sources

Two suppliers fill `core`. FTC Events is FIRST's own API and is authoritative. FTCScout is a third party that reads
FTC Events and republishes it, and offers an uncredentialed traffic allowance via GraphQL.

| | FTC Events | FTCScout |
| --- | --- | --- |
| Address | `https://ftc-api.firstinspires.org/v2.0` | `https://api.ftcscout.org/graphql` |
| Protocol | REST, JSON | GraphQL |
| Credentials | Basic auth, username and token | None |
| Pacing | 0.5 s between requests, 5 retries, `Retry-After` honored | 5 retries |
| Change detection | `If-Modified-Since`, falling back to the payload hash | Payload hash |
| Authority | Wins every disagreement | Fills a slot no FIRST row occupies |

## FTC Events endpoints

| Endpoint | Fills | Sends `Last-Modified` |
| --- | --- | --- |
| `/{season}/events` | `core.event` | yes |
| `/{season}/teams` | `core.team`, `core.team_season`, `core.event_team` | no |
| `/{season}/schedule/{event}/{level}/hybrid` | `core.match`, `core.match_team` | yes |
| `/{season}/scores/{event}/{level}` | `core.match_breakdown` | yes |
| `/{season}/rankings/{event}` | `core.ranking` | no |
| `/{season}/awards/{event}` | `core.award` | yes |
| `/{season}/alliances/{event}` | `core.playoff_alliance` | unverified |
| `/{season}/alliances/{event}/selection` | `core.playoff_alliance_pick` | unverified |
| `/{season}/advancement/{event}` | `core.event_advancement`, `core.advancement_slot` | no |
| `/{season}/advancement/{event}/points` | `core.advancement_points` | no |

An endpoint that sends no `Last-Modified` still returns full data. Cacheability and emptiness are separate axes, and
the cursor falls back to comparing the SHA-256 of the response bytes.

### Quirks

- **Every 2025 breakdown publishes `preFoulTotal = 0`.** DECODE's first ranking tiebreaker is `avg:preFoulTotal`,
  so a consumer that trusts the field sorts on zero for every team. `pub.v_breakdown_2025.pre_foul_total`
  republishes it as `autoPoints + teleopPoints`, declared by the rule pack as `recovered_from`.
  `core.match_breakdown` keeps what FIRST sent.
- **The OpenAPI document lives on the other host**, at
  `https://ftc-events.firstinspires.org/swagger/v2.0/swagger.json`, unauthenticated, rendered by ReDoc at
  `/api-docs`. Every swagger path on `ftc-api.firstinspires.org` redirects there and 404s, which reads as the
  document being unserved. `v2.0` in the URL is the API version, not the spec version. It carries
  `ScoreDetailAllianceModel_2020` through `_2025` and is what `rules generate` reads.
- **The `regionCode` parameter on `/{season}/events` is ignored.** It returns the worldwide list. Region filtering
  happens after the fetch.
- **`/{season}/awards/{event}` carries the sponsor in the name and FTCScout does not**, so no name is stored. The
  award code with the series identifies the award. Only judged team awards are kept, 11 codes.
- **`/{season}/teams` paginates at 500 rows**, 31 pages worldwide for 2025. A single-event query answers in one
  page, so an unpaged client looks like it is working.

## FTCScout queries

| Query | Fills |
| --- | --- |
| `eventsSearch` with matches, windowed by start date | `core.match` alliance totals, `core.match_team` |
| `eventsSearch` with alliance roles | `core.match_team.alliance_role`, by UPDATE only |
| `eventsSearch` with awards, one call per season | `core.award`, where FTC Events has no row |

`eventsSearch` takes a start and an end but no offset, so the backfill pages by date window. Re-running a window is
idempotent. Remote and hybrid events are dropped on arrival.

### Quirks

- **FTCScout publishes no `eventId`**, so event identity and team rosters have to land from FTC Events first. The
  backfill matches on the event code.
- **It names score components differently from FIRST**, `dcPoints` for `teleopPoints` and `movementRp` for
  `movementRP`, so the backfill writes alliance totals to `core.match` and nothing to `core.match_breakdown`.
- **It returns UTC where FTC Events returns venue-local.** The two paths derive the timestamp pair in opposite
  directions.
- **Its copy has been observed to drop values**, which is why FTC Events outranks it everywhere.
- **Awards arrive as an enum with no sponsor**, mapped to the 11 stored codes.

## Disagreements

`core.match` results come from FTCScout worldwide and `core.match_breakdown` from FTC Events, so the two tables are
two suppliers. The non-authoritative writes fill empty slots and never overwrite. A disagreement is logged to
`raw.ingest_conflict` as `source_disagreement` and the stored row stands.

Absence is not a disagreement. An empty `raw.ingest_conflict` means the comparison has not run, not that the
suppliers agree. `tests/test_cross_supplier.py` audits them against each other, under `pytest -m db`.

Award conflicts compare `team_number` alone, never the name. A null winner is dropped at ingest rather than stored.

## See also

- `../README.md`: schemas, roles and the command surface.
- `psql-guide.html`: local database setup and navigation.
