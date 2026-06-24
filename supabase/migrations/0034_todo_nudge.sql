-- Nudge: let a teammate poke the owner of an open to-do (bell + branded email).
-- `last_nudged_at` throttles re-nudging the same task in quick succession; the
-- API enforces the cooldown window (see app/routers/todos.py).
alter table todos add column last_nudged_at timestamptz;
