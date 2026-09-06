CREATE VIEW pub.v_ranking AS
SELECT
    r.event_id, e.season, e.code AS event_code, r.team_number, r.rank,
    r.wins, r.losses, r.ties, r.matches_played, r.qual_average, r.dq,
    r.sort_order_1, r.sort_order_2, r.sort_order_3,
    r.sort_order_4, r.sort_order_5, r.sort_order_6
FROM core.ranking r
JOIN core.event e ON e.event_id = r.event_id;

-- Only judged team awards are stored in the first place.
CREATE VIEW pub.v_award AS
SELECT a.event_id, e.season, e.code AS event_code, a.award_code, a.series, a.team_number
FROM core.award a
JOIN core.event e ON e.event_id = a.event_id;

-- Event size is inferred: it is the count of teams FTC Events listed at schedule generation, so a
-- forthcoming event has no size until its schedule posts.
CREATE VIEW pub.v_event_size AS
SELECT
    e.event_id,
    e.season,
    e.code AS event_code,
    count(et.team_number) AS teams_played
FROM core.event e
LEFT JOIN core.event_team et ON et.event_id = e.event_id
GROUP BY e.event_id, e.season, e.code;

CREATE VIEW pub.v_alliance_selection AS
SELECT
    a.event_id,
    e.season,
    e.code AS event_code,
    a.alliance_number AS seed,
    a.name,
    a.captain,
    slot.team_number,
    slot.role,
    r.rank AS qual_rank
FROM core.playoff_alliance a
JOIN core.event e ON e.event_id = a.event_id
CROSS JOIN LATERAL (VALUES
    ('captain', a.captain),
    ('round1', a.round1),
    ('round2', a.round2)
) AS slot(role, team_number)
LEFT JOIN core.ranking r ON r.event_id = a.event_id AND r.team_number = slot.team_number
WHERE slot.team_number IS NOT NULL;

CREATE VIEW pub.v_alliance_pick AS
SELECT p.event_id, e.season, e.code AS event_code, p.pick_ordinal, p.alliance_number, p.team_number, p.action
FROM core.playoff_alliance_pick p
JOIN core.event e ON e.event_id = p.event_id;
