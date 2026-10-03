-- Ordering is ordinal, Timestamps are untrusted and for display only.

CREATE VIEW pub.v_match_level_order AS
SELECT * FROM (VALUES
    ('PRACTICE'::core.match_level, 0),
    ('QUALIFICATION', 1),
    ('SEMIFINAL', 2),
    ('PLAYOFF', 2),
    ('FINAL', 3),
    ('OTHER', 4)
) AS t(level, level_rank);

CREATE VIEW pub.v_match AS
SELECT
    m.match_id,
    m.event_id,
    e.season,
    e.code AS event_code,
    e.date_start AS event_date_start,
    m.level,
    lo.level_rank,
    m.series,
    m.match_number,
    m.description,
    m.start_time_utc,
    m.start_time_local,
    m.actual_start_time_utc,
    m.actual_start_time_local,
    m.post_result_time_utc,
    m.post_result_time_local,
    m.score_red_final,
    m.score_blue_final,
    m.score_red_auto,
    m.score_blue_auto,
    m.score_red_foul,
    m.score_blue_foul
FROM core.match m
JOIN core.event e ON e.event_id = m.event_id
JOIN pub.v_match_level_order lo ON lo.level = m.level;

CREATE VIEW pub.v_match_team AS
SELECT
    mt.match_id,
    m.event_id,
    mt.station,
    mt.alliance,
    mt.team_number,
    mt.surrogate,
    mt.no_show,
    mt.dq,
    mt.on_field,
    mt.alliance_role
FROM core.match_team mt
JOIN core.match m ON m.match_id = mt.match_id;

-- The subset of matches that can be modeled.
CREATE VIEW pub.v_match_rating_input AS
WITH live AS (
    SELECT
        m.match_id,
        m.event_id,
        e.season,
        e.code AS event_code,
        e.type AS event_type,
        e.date_start AS event_date_start,
        m.level,
        lo.level_rank,
        m.series,
        m.match_number,
        m.score_red_final,
        m.score_blue_final,
        m.score_red_auto,
        m.score_blue_auto,
        m.score_red_foul,
        m.score_blue_foul
    FROM core.match m
    JOIN core.event e ON e.event_id = m.event_id
    JOIN pub.v_match_level_order lo ON lo.level = m.level
    WHERE m.level <> 'PRACTICE'
      AND lower(coalesce(e.type, '')) NOT LIKE '%scrimmage%'
      AND coalesce(e.type, '') NOT IN ('Off-Season', 'Premier')
      AND m.score_red_final IS NOT NULL
      AND m.score_blue_final IS NOT NULL
),
ordered AS (
    SELECT
        live.*,
        row_number() OVER (
            PARTITION BY live.event_id
            ORDER BY live.level_rank, live.series, live.match_number
        ) AS event_match_ordinal
    FROM live
),
sides AS (
    SELECT o.*, a.alliance FROM ordered o
    CROSS JOIN (VALUES ('RED'::core.alliance), ('BLUE'::core.alliance)) AS a(alliance)
),
official AS NOT MATERIALIZED (
    SELECT match_id, alliance,
        (breakdown ->> 'totalPoints')::double precision AS total
    FROM core.match_breakdown
),
resolved AS (
    SELECT
        s.*,
        coalesce(
            own.total,
            CASE s.alliance WHEN 'RED' THEN s.score_red_final ELSE s.score_blue_final END
        ) AS official_score,
        coalesce(
            opp.total,
            CASE s.alliance WHEN 'RED' THEN s.score_blue_final ELSE s.score_red_final END
        ) AS official_opponent_score
    FROM sides s
    LEFT JOIN official own ON own.match_id = s.match_id AND own.alliance = s.alliance
    LEFT JOIN official opp ON opp.match_id = s.match_id AND opp.alliance <> s.alliance
)
SELECT
    s.match_id,
    s.event_id,
    s.season,
    s.event_code,
    s.event_type,
    s.event_date_start,
    s.level,
    s.series,
    s.match_number,
    s.event_match_ordinal,
    s.alliance,
    CASE s.alliance WHEN 'RED' THEN s.score_red_final ELSE s.score_blue_final END AS score,
    CASE s.alliance WHEN 'RED' THEN s.score_blue_final ELSE s.score_red_final END AS opponent_score,
    s.official_score,
    s.official_opponent_score,
    CASE
        WHEN s.official_score > s.official_opponent_score THEN 'WIN'
        WHEN s.official_score < s.official_opponent_score THEN 'LOSS'
        ELSE 'TIE'
    END AS official_result,
    CASE s.alliance WHEN 'RED' THEN s.score_red_auto ELSE s.score_blue_auto END AS score_auto,
    CASE s.alliance WHEN 'RED' THEN s.score_red_foul ELSE s.score_blue_foul END
        AS score_foul_committed,
    CASE s.alliance WHEN 'RED' THEN s.score_blue_foul ELSE s.score_red_foul END
        AS score_foul_received,
    CASE s.alliance
        WHEN 'RED' THEN s.score_red_final - coalesce(s.score_blue_foul, 0)
        ELSE s.score_blue_final - coalesce(s.score_red_foul, 0)
    END AS score_no_foul,
    array_agg(mt.team_number ORDER BY mt.station) AS team_numbers,
    array_agg(mt.surrogate ORDER BY mt.station) AS surrogates,
    array_agg(mt.no_show ORDER BY mt.station) AS no_shows,
    array_agg(mt.dq ORDER BY mt.station) AS dqs
FROM resolved s
JOIN core.match_team mt ON mt.match_id = s.match_id AND mt.alliance = s.alliance
GROUP BY
    s.match_id, s.event_id, s.season, s.event_code, s.event_type,
    s.event_date_start, s.level, s.series, s.match_number,
    s.event_match_ordinal, s.alliance, s.score_red_final, s.score_blue_final,
    s.score_red_auto, s.score_blue_auto, s.score_red_foul, s.score_blue_foul,
    s.official_score, s.official_opponent_score;
