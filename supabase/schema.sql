-- Run this once in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.
-- Row-level security is ON for every table and there are NO policies, so the public
-- (publishable / anon) key can read and write nothing. Only the secret key, which the bot on
-- your phone and your Vercel project hold, can use these tables.

-- One row (id = 1): the latest status snapshot the phone pushes for the remote dashboard.
create table if not exists public.bot_status (
  id          int primary key check (id = 1),
  payload     jsonb not null,
  updated_at  timestamptz not null default now()
);

-- Buttons pressed on the Vercel dashboard. The phone polls for pending ones and runs them.
create table if not exists public.bot_commands (
  id          bigint generated always as identity primary key,
  command     text not null check (command in ('start_demo', 'stop_demo', 'kill')),
  status      text not null default 'pending' check (status in ('pending', 'done', 'error', 'expired')),
  result      text,
  created_at  timestamptz not null default now(),
  handled_at  timestamptz
);
create index if not exists bot_commands_pending_idx on public.bot_commands (status, id);

-- Copy of the phone's saved state (flags, demo clock, trade count, open position...).
-- API keys are included only if SUPABASE_SYNC_SECRETS=1 on the phone, and then only in their
-- encrypted form.
create table if not exists public.state_backup (
  key         text primary key,
  value       text not null,
  updated_at  timestamptz not null default now()
);

-- Copy of every closed trade.
create table if not exists public.trade_backup (
  id          bigint primary key,
  data        jsonb not null,
  updated_at  timestamptz not null default now()
);

alter table public.bot_status    enable row level security;
alter table public.bot_commands  enable row level security;
alter table public.state_backup  enable row level security;
alter table public.trade_backup  enable row level security;
