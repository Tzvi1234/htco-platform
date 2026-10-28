-- Policy engine v1 (slice 7.2, FR-IAM-07).
--
-- `app.policies`: each organization's versioned policy document (`htco_domain.policy`): the
-- capability catalogue with side effects and risk, and the grants of employees, automations and
-- system components. A change is a new row with the next version, never an edit (AC-7.2.3); the
-- API publishes it after its own check (Owner, aal2, step-up) and audits it in the same
-- transaction. Rows are append-only.
--
-- `app.policy_decisions`: every decision of the engine with the version it was made under (null
-- only when the organization had no policy yet), append-only. A row holds identifiers, the
-- capability, the verdict and its reason: never request parameters or buyer data.
--
-- The Owner and Admins read their organization's rows; no member writes. The worker reads its
-- job's organization's policy and records that organization's decisions.
--
-- Safe on the live database: new tables, functions and policies.

create table app.policies (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  version integer not null constraint policies_version_check check (version >= 1),
  document jsonb not null constraint policies_document_check check (
    jsonb_typeof(document) = 'object' and pg_column_size(document) <= 262144
  ),
  created_by_kind text not null
    constraint policies_created_by_kind_check check (created_by_kind in ('user', 'system')),
  created_by_id text not null
    constraint policies_created_by_id_check check (char_length(btrim(created_by_id)) between 1 and 200),
  created_at timestamptz not null,
  constraint policies_version_key unique (organization_id, version)
);

create table app.policy_decisions (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  policy_version integer,
  shop_id uuid,
  actor_kind text not null constraint policy_decisions_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null
    constraint policy_decisions_actor_id_check check (char_length(actor_id) between 1 and 200),
  capability text not null
    constraint policy_decisions_capability_check check (char_length(capability) <= 200),
  verdict text not null
    constraint policy_decisions_verdict_check check (verdict in ('allow', 'require_approval', 'deny')),
  reason text not null constraint policy_decisions_reason_check check (
    reason in ('granted', 'pre_approved', 'role_requires_approval', 'acts_outside_htco',
               'critical_risk', 'no_policy', 'unknown_capability', 'unknown_actor',
               'advisor_has_no_capabilities', 'not_granted', 'other_organization',
               'shop_not_granted', 'no_originating_user', 'autonomy_too_low', 'mfa_required',
               'step_up_required')
  ),
  correlation_id text not null
    constraint policy_decisions_correlation_id_check check (char_length(correlation_id) between 1 and 200),
  decided_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint policy_decisions_policy_fkey foreign key (organization_id, policy_version)
    references app.policies (organization_id, version) on delete cascade,
  -- A removed shop keeps its decisions, without the shop.
  constraint policy_decisions_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete set null (shop_id),
  -- The reason fixes the verdict (htco_domain.policy.Reason.verdict).
  constraint policy_decisions_verdict_reason_check check (
    case
      when reason in ('granted', 'pre_approved') then verdict = 'allow'
      when reason in ('role_requires_approval', 'acts_outside_htco', 'critical_risk')
        then verdict = 'require_approval'
      else verdict = 'deny'
    end
  ),
  constraint policy_decisions_version_check check ((policy_version is null) = (reason = 'no_policy'))
);
create index policy_decisions_organization_decided_idx
  on app.policy_decisions (organization_id, decided_at desc);
create index policy_decisions_version_idx on app.policy_decisions (organization_id, policy_version);
create index policy_decisions_shop_idx on app.policy_decisions (shop_id);

-- Append-only, except when the organization itself is removed (a cascade runs at depth > 0).
create function private.reject_policy_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception '% is append-only', tg_table_name using errcode = '42501';
end;
$$;
revoke all on function private.reject_policy_change() from public;
create trigger policies_append_only before update or delete on app.policies
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_policy_change();
create trigger policy_decisions_append_only before update or delete on app.policy_decisions
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_policy_change();

alter table app.policies enable row level security;
alter table app.policy_decisions enable row level security;
revoke all on app.policies, app.policy_decisions from public, anon, authenticated, service_role;

-- The Owner and Admins: their organization's versions and decisions.
grant select on app.policies, app.policy_decisions to authenticated;
create policy policies_select on app.policies for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
create policy policy_decisions_select on app.policy_decisions for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));

-- The worker: reads its job's policy, records its job's decisions.
grant select on app.policies to htco_worker;
create policy policies_worker on app.policies for select to htco_worker
  using (organization_id = (select private.worker_organization_id()));
grant select, insert on app.policy_decisions to htco_worker;
create policy policy_decisions_worker on app.policy_decisions for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
