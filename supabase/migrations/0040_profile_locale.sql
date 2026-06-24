-- Per-user UI / notification language preference.
--
-- Self-service: any user sets their own via PATCH /users/me (the app constrains
-- the value to the supported locale set). Defaults to English so existing rows
-- and any user who never changes it keep the current behaviour. External comms
-- (vendor RFQs, GC proposals) are NOT affected — only the user's own UI and the
-- notifications they receive read this.

alter table public.profiles
  add column if not exists locale text not null default 'en';

-- Keep the column honest at the DB layer too. Mirrors SUPPORTED_LOCALES in the
-- frontend (lib/locales.ts) and the backend SupportedLocale Literal; updating the
-- shipped languages means updating all three.
alter table public.profiles
  drop constraint if exists profiles_locale_check;
alter table public.profiles
  add constraint profiles_locale_check
  check (locale in ('en', 'fil', 'ceb', 'sw', 'hi', 'ur'));

-- Reload PostgREST's schema cache so the new column is exposed immediately.
notify pgrst, 'reload schema';
