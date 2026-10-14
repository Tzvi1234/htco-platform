-- Test helpers, committed into the throwaway test database before the other files run
-- (files run in name order). Our own small set instead of a downloaded helper library:
--   tests.create_user(email)            a Supabase Auth user, returns its id
--   tests.authenticate_as(id, aal)      act as that user, as the API does (role + JWT claims)
-- Use `reset role;` to act as the test owner again.
begin;
create extension if not exists pgtap with schema extensions;

create schema if not exists tests;

create or replace function tests.create_user(email text) returns uuid
language sql as $$
  insert into auth.users (
    id, instance_id, aud, role, email, raw_app_meta_data, raw_user_meta_data,
    created_at, updated_at
  )
  values (
    gen_random_uuid(), '00000000-0000-0000-0000-000000000000', 'authenticated',
    'authenticated', email, '{}'::jsonb, '{}'::jsonb, now(), now()
  )
  returning id
$$;

create or replace function tests.authenticate_as(user_id uuid, aal text default 'aal2')
returns void
language plpgsql as $$
begin
  perform set_config(
    'request.jwt.claims',
    json_build_object('sub', user_id, 'role', 'authenticated', 'aal', aal)::text,
    true
  );
  perform set_config('role', 'authenticated', true);
end;
$$;

select plan(2);
select has_function('tests', 'create_user', array['text']);
select has_function('tests', 'authenticate_as', array['uuid', 'text']);
select * from finish();
commit;
