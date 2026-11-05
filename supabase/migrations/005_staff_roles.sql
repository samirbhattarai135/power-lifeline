-- Role-based access: which signed-in staff member may do what.
-- Run once after 004, in the Supabase dashboard: SQL Editor > New query > paste > Run.
--
-- Accounts (email + password) stay in Supabase Auth; this table only says what each
-- email may do in Lifeline. An email with no row here can sign in but sees nothing.
--   admin  - everything: patients, staff, unit tokens, family links
--   nurse  - live board, "We're responding", patient details and call lists, history
--   viewer - live board and history, read only
--   county - the county emergency-management view only
-- Same access model as the other tables: only the server's secret key touches it.

create table if not exists public.staff_roles (
  email      text primary key check (
               email = lower(email) and length(email) <= 254 and email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
  role       text not null check (role in ('admin', 'nurse', 'viewer', 'county')),
  created_at timestamptz not null default now()
);

alter table public.staff_roles enable row level security;
revoke all on table public.staff_roles from anon, authenticated;
grant select, insert, update, delete on table public.staff_roles to service_role;
