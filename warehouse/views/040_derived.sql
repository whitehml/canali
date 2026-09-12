-- Model output: OPR, EPA and pRidge ratings, and the runs that produced them. A rating carries every model version
-- and every prior version it was fitted against, so a read is pinned by joining pub.v_fit_run on fit_run_id.

-- opr_teleop is the remainder opr_total_np - opr_auto, so endgame is folded into teleop.
CREATE VIEW pub.v_opr AS
SELECT o.event_id, e.season, e.code AS event_code, s.event_ordinal,
       o.team_number, o.opr_total, o.opr_total_np, o.opr_auto, o.opr_teleop
FROM derived.team_event_opr o
JOIN core.event e ON e.event_id = o.event_id
JOIN pub.v_event_sequence s ON s.event_id = o.event_id;

CREATE VIEW pub.v_team_pridge AS
SELECT r.fit_run_id, r.season, r.team_number, r.event_id, r.as_of_match,
       r.model_version, r.component, r.pridge, r.lambda_
FROM derived.team_pridge r;

-- tag is the moment a rating was taken at: season_start, pre_event, post_event or match.
CREATE VIEW pub.v_team_epa AS
SELECT fit_run_id, season, team_number, event_id, tag, as_of_match,
       model_version, epa_norm, epa_scaled, scale_provisional, components
FROM derived.team_epa;

-- Completed batch runs. A run appears once it finishes, and a live event-scope run is not published here.
CREATE VIEW pub.v_fit_run AS
SELECT f.model, f.season, f.model_version, f.prior_version, f.fit_run_id,
       f.started_at_utc, f.finished_at_utc
FROM derived.fit_run f
WHERE f.event_id IS NULL AND f.finished_at_utc IS NOT NULL;

CREATE VIEW pub.v_team_epa_pre_event AS
SELECT t.season, t.event_id, s.event_ordinal, t.team_number, t.model_version,
       t.epa_scaled, t.epa_norm, t.scale_provisional, t.components, t.fit_run_id
FROM derived.team_epa t
JOIN pub.v_event_sequence s ON s.event_id = t.event_id
WHERE t.tag = 'pre_event';
