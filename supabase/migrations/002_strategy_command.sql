-- Run once in the Supabase SQL Editor if you created the tables with the first version of
-- schema.sql (before the Vercel page could switch strategy). Safe to run twice.
alter table public.bot_commands add column if not exists arg text;
alter table public.bot_commands drop constraint if exists bot_commands_command_check;
alter table public.bot_commands
  add constraint bot_commands_command_check
  check (command in ('start_demo', 'stop_demo', 'kill', 'select_strategy'));
