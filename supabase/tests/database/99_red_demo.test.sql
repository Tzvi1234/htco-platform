-- Deliberately failing test, pushed only to show that a failing pgTAP test fails CI
-- (AC-2.1.2). It is reverted in the next commit.
begin;
create extension if not exists pgtap with schema extensions;
select plan(1);
select is(1, 2, 'AC-2.1.2 demonstration: this assertion fails on purpose');
select * from finish();
rollback;
