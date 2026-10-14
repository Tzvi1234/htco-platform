-- Web sessions and sign-in attempts (slice 3.5): server-only tables in `private`.
begin;
create extension if not exists pgtap with schema extensions;
select plan(10);

select has_table('private', 'web_sessions', 'private.web_sessions exists');
select has_table('private', 'sign_in_attempts', 'private.sign_in_attempts exists');
select ok(
  (select relrowsecurity from pg_class where oid = 'private.web_sessions'::regclass),
  'web_sessions has RLS enabled (and no policy: no client role reads it)'
);
select ok(
  (select relrowsecurity from pg_class where oid = 'private.sign_in_attempts'::regclass),
  'sign_in_attempts has RLS enabled'
);
select ok(not has_table_privilege('authenticated', 'private.web_sessions', 'select'),
  'authenticated cannot read sessions');
select ok(not has_table_privilege('anon', 'private.web_sessions', 'select'),
  'anon cannot read sessions');
select ok(not has_table_privilege('service_role', 'private.web_sessions', 'select'),
  'service_role cannot read sessions either');
select col_type_is('private', 'web_sessions', 'access_token', 'bytea',
  'tokens are stored sealed (bytes), never as text');
select col_is_unique('private', 'web_sessions', 'token_hash',
  'one session per cookie hash');
select throws_ok(
  $$ insert into private.web_sessions
       (id, token_hash, user_id, access_token, refresh_token, access_expires_at,
        idle_expires_at, absolute_expires_at)
     values (gen_random_uuid(), '\x00', gen_random_uuid(), '\x00', '\x00', now(), now(), now()) $$,
  '23503', null, 'a session belongs to a real user'
);

select * from finish();
rollback;
