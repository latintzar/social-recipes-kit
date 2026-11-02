-- Optional JSON metadata on chat threads (location tokens, email bridge, IG flags).
alter table if exists chat_threads
  add column if not exists metadata jsonb not null default '{}'::jsonb;
