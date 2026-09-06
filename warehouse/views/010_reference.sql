-- Reference surface: seasons, events, teams, and the rule packs that decode season-shaped JSONB.

CREATE VIEW pub.v_event AS
SELECT
    e.event_id,
    e.season,
    e.code,
    e.name,
    e.type,
    e.division_code,
    e.region_code,
    e.league_code,
    e.field_count,
    e.date_start,
    e.date_end,
    e.venue,
    e.city,
    e.state_prov,
    e.country,
    e.timezone,
    e.timezone_assumed
FROM core.event e;

CREATE VIEW pub.v_team AS
SELECT team_number, rookie_year FROM core.team;

CREATE VIEW pub.v_team_season AS
SELECT
    ts.season,
    ts.team_number,
    ts.name_full,
    ts.name_short,
    -- TIMS pass-through, nullable and unvalidated. FIRST's regional assignment, home_region, is the authoritative
    -- eligibility field.
    ts.home_state AS from_state,
    ts.home_country AS from_country,
    ts.city AS from_city,
    ts.home_region AS eligibility_region
FROM core.team_season ts;

CREATE VIEW pub.v_rule_pack AS
SELECT
    season, game, version, rp_win, rp_tie, rp_loss, has_bonus_rp,
    alliance_size, ranking_formula, tiebreakers,
    playoff_structure, alliance_brackets, playoff_implemented,
    advancement_implemented
FROM core.rule_pack;

CREATE VIEW pub.v_rule_pack_component AS
SELECT
    season, name, column_name, level, kind,
    (kind IN ('numeric', 'boolean') AND NOT is_derived) AS fittable,
    is_subtotal, is_derived, partition_group, recovered_from
FROM core.rule_pack_component;

-- Deterministic, but arbitrary order for events that start on the same day.
CREATE VIEW pub.v_event_sequence AS
SELECT event_id, season, code AS event_code, date_start,
       row_number() OVER (PARTITION BY season ORDER BY date_start, code) AS event_ordinal
FROM core.event;
