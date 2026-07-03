-- 0047 — Go/No-Go: the score decides; voting retired.
--
-- The intake rubric score now drives the outcome when a project enters the
-- go_no_go stage: >= 30 auto-Go (straight to To Estimator), 20-29 parks in
-- review for a manual decision, < 20 auto-No-Go (declined). Any writer role
-- may push review/go/no_go regardless of the score. Decision methods gain
-- 'score' (auto-applied from the rubric) and 'manual' (a user pushed the
-- outcome); 'majority' and 'override' survive only on historical rows.
--
-- ADD VALUE is safe inside the migration transaction on PG >= 12 because the
-- new values are not used within this same transaction.
alter type gono_method add value if not exists 'score';
alter type gono_method add value if not exists 'manual';

-- Votes are gone. Individual vote history lives on in audit_log ('gono.vote').
drop table if exists go_no_go_votes;
