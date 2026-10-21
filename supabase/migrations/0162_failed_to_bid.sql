-- 0162 - Failed to bid. A bid we MISSED (as opposed to one we chose to walk
-- away from) is recorded with the same abandon marker (0039), so it freezes in
-- place, drops off every work list and closes the estimator portal exactly
-- like an abandon. These columns record that it was a miss and why. The
-- reason is required by the API: a coded `failed_to_bid_reason` for reporting
-- plus a free-text `failed_to_bid_note` explaining what happened. Reactivate
-- clears all four along with the abandon marker.
alter table projects add column if not exists failed_to_bid_at timestamptz;
-- SET NULL keeps the record if the actor's profile is later removed (as 0039).
alter table projects add column if not exists failed_to_bid_by uuid
  references profiles(id) on delete set null;
alter table projects add column if not exists failed_to_bid_reason text
  check (failed_to_bid_reason is null or failed_to_bid_reason in (
    'missed_deadline',
    'late_plans',
    'missing_quotes',
    'estimate_not_finished',
    'capacity',
    'submission_problem',
    'other'
  ));
alter table projects add column if not exists failed_to_bid_note text;
