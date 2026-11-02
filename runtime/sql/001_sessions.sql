-- Apply in your own Supabase project. This dedicated schema stores only your IG sessions.
-- Recipe runtime uses the service-role client and verifies ownership in chat_threads.py.
-- Add social_recipes to your project's exposed API schemas after reviewing this migration.
begin;
create schema if not exists social_recipes;
revoke all on schema social_recipes from public, anon, authenticated;
grant usage on schema social_recipes to service_role;
create table if not exists social_recipes.chat_threads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid,
  cart_key text,
  title text,
  pinned boolean not null default false,
  seeded_from_order_id uuid,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint social_recipes_thread_owner_present check (user_id is not null or cart_key is not null)
);
create index if not exists social_recipes_threads_key_idx on social_recipes.chat_threads(cart_key, updated_at desc);
create table if not exists social_recipes.chat_messages (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references social_recipes.chat_threads(id) on delete cascade,
  role text not null check (role in ('user','assistant','tool','system')),
  content jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists social_recipes_messages_thread_idx on social_recipes.chat_messages(thread_id, created_at);
alter table social_recipes.chat_threads enable row level security;
alter table social_recipes.chat_messages enable row level security;
revoke all on all tables in schema social_recipes from public, anon, authenticated;
grant select, insert, update, delete on all tables in schema social_recipes to service_role;
comment on schema social_recipes is 'Server-only Instagram recipe-agent sessions; transport derives sender ownership after webhook HMAC verification';
commit;
