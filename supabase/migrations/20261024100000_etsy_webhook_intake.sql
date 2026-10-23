-- Etsy webhooks (slice 5.3): the worker starts Etsy Order Intake from a webhook's job.
--
-- The `etsy.webhook_event` job runs as `htco_worker` inside the event's organization. For an order
-- event it triggers the `etsy-order-intake` automation through the durable engine, which inserts the
-- execution, its first state change and its first job in one transaction. Until now only the API's
-- login role created executions (user triggers, slice 5.10); the worker could read and advance them.
-- It may now insert them too, for its job's organization only. The inbox gets an index on its job,
-- so the job marks the events it handled processed without a scan.
--
-- Safe on the live database: a grant, a policy and an index on a small table (no data change).

grant insert on app.automation_executions to htco_worker;
create policy automation_executions_worker_insert on app.automation_executions for insert
  to htco_worker
  with check (organization_id = (select private.worker_organization_id()));

create index inbox_job_id_idx on jobs.inbox (job_id) where job_id is not null;
