-- Run only against your disposable empty test database.
\set ON_ERROR_STOP on
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
END $$;
\ir ../../runtime/sql/001_sessions.sql
DO $$ BEGIN
  IF has_schema_privilege('anon','social_recipes','USAGE') OR has_schema_privilege('authenticated','social_recipes','USAGE') THEN RAISE EXCEPTION 'Browser roles can access sessions'; END IF;
  IF has_table_privilege('anon','social_recipes.chat_threads','SELECT') OR has_table_privilege('authenticated','social_recipes.chat_messages','SELECT') THEN RAISE EXCEPTION 'Browser roles can read sessions'; END IF;
  IF (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname='social_recipes' AND c.relname IN ('chat_threads','chat_messages') AND c.relrowsecurity) <> 2 THEN RAISE EXCEPTION 'Session RLS is missing'; END IF;
END $$;
SET ROLE service_role;
INSERT INTO social_recipes.chat_threads(id,cart_key) VALUES ('00000000-0000-4000-8000-000000000001','ig:fictional-test');
INSERT INTO social_recipes.chat_messages(thread_id,role,content) VALUES ('00000000-0000-4000-8000-000000000001','user','{"text":"Fictional recipe question"}');
DO $$ BEGIN
  IF (SELECT count(*) FROM social_recipes.chat_messages WHERE thread_id='00000000-0000-4000-8000-000000000001') <> 1 THEN RAISE EXCEPTION 'Service session write/read failed'; END IF;
END $$;
RESET ROLE;
-- Re-applying the bootstrap preserves the records and access boundary.
\ir ../../runtime/sql/001_sessions.sql
