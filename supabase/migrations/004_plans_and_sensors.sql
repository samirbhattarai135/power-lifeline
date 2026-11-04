-- Emergency plans, the machine-running sensor, and three new kinds of incident.
-- Run once after 003, in the Supabase dashboard: SQL Editor > New query > paste > Run.

-- What to do when the power goes out: backup supplies, where to go, and the steps.
alter table public.patients add column if not exists plan jsonb not null default '{}'
  check (jsonb_typeof(plan) = 'object');

-- With a current clamp on the equipment's cord: below this many amps while the wall has
-- power, the machine has stopped. Null means no clamp is fitted.
alter table public.patients add column if not exists running_amps_min real
  check (running_amps_min is null or running_amps_min between 0.05 and 30);

-- machine: the equipment stopped although the power is on
-- help: someone pressed the help button on the unit
-- temperature: the room got dangerously hot or cold
alter table public.outages drop constraint if exists outages_kind_check;
alter table public.outages add constraint outages_kind_check
  check (kind in ('power', 'silent', 'storm', 'machine', 'help', 'temperature'));
