-- When the whole call list was told a patient's backup is almost out.
-- Run once after 005, in the Supabase dashboard: SQL Editor > New query > paste > Run.
-- Until it is run, incidents save as before, except one that reaches this warning: its later
-- saves are refused (and logged), so the history would miss how it ended.

alter table public.outages add column if not exists backup_low_at timestamptz;
