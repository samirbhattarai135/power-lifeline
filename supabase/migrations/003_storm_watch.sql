-- Storm Watch: storm notices sent before an outage are kept as a third kind of incident.
-- Run once after 002, in the Supabase dashboard: SQL Editor > New query > paste > Run.

alter table public.outages drop constraint if exists outages_kind_check;
alter table public.outages add constraint outages_kind_check check (kind in ('power', 'silent', 'storm'));

-- detail: the warning shown in the history, e.g. "Ice Storm Warning".
-- hazard_id: which hazard was announced, so a restarted server doesn't announce it again.
alter table public.outages add column if not exists detail text not null default ''
  check (length(detail) <= 200);
alter table public.outages add column if not exists hazard_id text not null default ''
  check (length(hazard_id) <= 300);
