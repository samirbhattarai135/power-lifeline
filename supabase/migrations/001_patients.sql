-- Lifeline patient roster.
-- Run once in the Supabase dashboard: SQL Editor > New query > paste > Run.
--
-- Access model: only the Lifeline server touches this table, using the project's
-- secret (service_role) key, which bypasses row level security. RLS is enabled
-- with NO policies, so the public anon/publishable key and signed-in browser
-- sessions can read and write nothing here.

create table if not exists public.patients (
  device_id      text primary key check (device_id ~ '^[a-z0-9][a-z0-9-]{2,39}$'),
  device_token   text not null check (length(device_token) >= 16),
  share_token    text not null unique check (length(share_token) >= 16),
  name           text not null check (length(name) between 1 and 120),
  address        text not null check (length(address) between 1 and 120),
  lat            double precision not null check (lat between -90 and 90),
  lon            double precision not null check (lon between -180 and 180),
  equipment      text not null check (length(equipment) between 1 and 120),
  backup_minutes integer not null check (backup_minutes between 0 and 2880),
  acuity         smallint not null check (acuity between 1 and 3),
  contacts       jsonb not null check (
                   jsonb_typeof(contacts) = 'array'
                   and jsonb_array_length(contacts) between 1 and 6
                 ),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create or replace function public.touch_patients_updated_at()
returns trigger language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists patients_touch_updated_at on public.patients;
create trigger patients_touch_updated_at
  before update on public.patients
  for each row execute function public.touch_patients_updated_at();

alter table public.patients enable row level security;
revoke all on table public.patients from anon, authenticated;
-- Newer projects don't expose SQL-created tables automatically, so grant the
-- server's role explicitly. anon/authenticated stay locked out.
grant select, insert, update, delete on table public.patients to service_role;
