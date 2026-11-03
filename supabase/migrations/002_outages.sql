-- Lifeline incidents: power outages and silent units, one row each.
-- Run once in the Supabase dashboard: SQL Editor > New query > paste > Run.
--
-- The server saves a row on every change (alert sent, escalation, acknowledgment,
-- end). Open rows (ended_at is null) let a restarted server resume escalation;
-- all rows feed the outage history and the report for inspectors.
--
-- Same access model as patients: only the server's secret key touches it.
-- No foreign key to patients on purpose: the history must outlive a removed patient,
-- so the patient's name and equipment are copied into each row.

create table if not exists public.outages (
  id                 uuid primary key,
  kind               text not null check (kind in ('power', 'silent')),
  device_id          text not null check (device_id ~ '^[a-z0-9][a-z0-9-]{2,39}$'),
  patient_name       text not null check (length(patient_name) between 1 and 120),
  equipment          text not null check (length(equipment) between 1 and 120),
  started_at         timestamptz not null,
  ended_at           timestamptz check (ended_at is null or ended_at >= started_at),
  reply_code         text not null default '' check (reply_code ~ '^([0-9]{4})?$'),
  escalation_step    integer not null default 0 check (escalation_step >= 0),
  next_escalation_at timestamptz,
  acknowledged_by    text check (length(acknowledged_by) <= 200),
  acknowledged_at    timestamptz,
  alerted            jsonb not null default '[]' check (jsonb_typeof(alerted) = 'array'),
  updated_at         timestamptz not null default now()
);

-- Restart recovery reads open incidents; the history reads by start time.
create index if not exists outages_open_idx on public.outages (device_id) where ended_at is null;
create index if not exists outages_started_at_idx on public.outages (started_at desc);

create or replace function public.touch_outages_updated_at()
returns trigger language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists outages_touch_updated_at on public.outages;
create trigger outages_touch_updated_at
  before update on public.outages
  for each row execute function public.touch_outages_updated_at();

alter table public.outages enable row level security;
revoke all on table public.outages from anon, authenticated;
grant select, insert, update, delete on table public.outages to service_role;
