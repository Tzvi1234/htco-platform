"""`GET /v1/operations/health` and `POST /v1/operations/dead-letters/{job_id}/retry` (slice 5.9):
the Owner's and Admins' view of the queue, executions and syncs, and the audited retry."""

import uuid
from datetime import datetime

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.idempotency import STAGED_KEY_REQUIRED
from htco_application.access.authentication import Membership
from htco_application.audit import AuditRecord
from htco_application.operations_health import (
    EXECUTION_JOB_KIND,
    ExecutionStat,
    FoundJob,
    HealthSnapshot,
)
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.application.test_email_failures import INCIDENT, MEMBER, MemoryFailedEmails, failure
from tests.application.test_operations_health import (
    EXECUTION,
    JOB,
    NOW,
    Jobs,
    Reader,
    dead,
    snapshot,
)
from tests.application.test_shop_dashboard import ORG, SHOP
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.idempotency_fakes import MemoryIdempotencyStore, fresh_idempotency_key

pytestmark = pytest.mark.anyio


class Operations:
    """Both ports in one, as the Postgres adapter is."""

    def __init__(self, reader: Reader, jobs: Jobs) -> None:
        self.reader, self.jobs = reader, jobs

    def snapshot(self, organization_id: uuid.UUID, *, now: datetime) -> HealthSnapshot:
        return self.reader.snapshot(organization_id, now=now)

    def find(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> FoundJob | None:
        return self.jobs.find(organization_id, job_id)

    def retry(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, *, now: datetime, audit: AuditRecord
    ) -> bool:
        return self.jobs.retry(organization_id, job_id, now=now, audit=audit)


def client(  # noqa: PLR0913 - the ports each test wires or leaves out
    role: Role = Role.OWNER, *, jobs: Jobs | None = None, reader: Reader | None = None,
    wired: bool = True, replays: bool = True, emails: MemoryFailedEmails | None = None,
) -> httpx.AsyncClient:  # fmt: skip
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                verifier, Memberships(Membership(ORG, role, frozenset({SHOP})))
            ),
            operations=Operations(reader or Reader(), jobs or dead()) if wired else None,
            idempotency=MemoryIdempotencyStore() if replays else None,
            failed_emails=(emails or MemoryFailedEmails()) if wired else None,
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
        event_hooks={"request": [fresh_idempotency_key]},
    )


@pytest.mark.ac("AC-5.9.2")
@pytest.mark.ac("AC-5.9.1")
@pytest.mark.ac("AC-5.9.3")
async def test_health__owner__every_part_with_ids_states_and_errors_only() -> None:
    stats = (ExecutionStat("etsy-order-intake", "succeeded", 2, 4.0),)
    async with client(reader=Reader(snapshot(execution_stats=stats))) as http:
        answer = await http.get("/v1/operations/health")

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["organization_id"] == str(ORG)
    assert body["dead_letters"][0]["last_error"] == "MarketplaceUnavailableError"
    assert [(w["worker_id"], w["alive"]) for w in body["workers"]] == [
        ("agent-worker:a", True),
        ("agent-worker:b", False),
    ]
    assert body["shops"][0]["stale"] is False
    assert body["automations"] == [
        {
            "automation_id": "etsy-order-intake",
            "executions": 1,
            "succeeded": 1,
            "dead_lettered": 0,
            "retries": 1,
            "success_rate": "100.0",
            "p50_seconds": 4.0,
            "p95_seconds": 4.0,
        }
    ]
    assert {"jobs", "webhooks", "executions", "outbox_failures"} <= body.keys()
    assert answer.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.VIEWER])
async def test_health__other_roles__403(role: Role) -> None:
    async with client(role) as http:
        answer = await http.get("/v1/operations/health")

    assert answer.status_code == 403


async def test_health__not_wired__503() -> None:
    async with client(wired=False) as http:
        answer = await http.get("/v1/operations/health")

    assert answer.status_code == 503


@pytest.mark.ac("AC-5.9.2")
async def test_retry__a_dead_job__202_and_audited() -> None:
    jobs = dead()
    async with client(Role.ADMIN, jobs=jobs) as http:
        answer = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")

    assert answer.status_code == 202, answer.text
    assert answer.json() == {"job_id": str(JOB), "state": "queued"}
    [audit] = jobs.audits
    assert audit.action == "job.dead_letter_retried"
    assert audit.correlation_id == answer.headers["X-Request-Id"]


async def test_retry__same_idempotency_key__retried_once_and_replayed() -> None:
    """Architecture audit 6.1: a timed-out client retry gets the first answer, not a second
    retry (or a 409 job-not-dead once the job is queued again)."""
    jobs = dead()
    headers = {"Idempotency-Key": "retry-1"}
    async with client(Role.ADMIN, jobs=jobs) as http:
        first = await http.post(f"/v1/operations/dead-letters/{JOB}/retry", headers=headers)
        again = await http.post(f"/v1/operations/dead-letters/{JOB}/retry", headers=headers)

    assert [first.status_code, again.status_code] == [202, 202]
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(jobs.audits) == 1


async def test_retry__same_key_for_another_organization__422_and_not_retried() -> None:
    """The fingerprint covers the query: the same key with another `?organization_id=` is a
    different request (422 idempotency-key-reused), not the first organization's answer."""
    jobs = dead()
    headers = {"Idempotency-Key": "retry-org"}
    url = f"/v1/operations/dead-letters/{JOB}/retry"
    async with client(Role.ADMIN, jobs=jobs) as http:
        first = await http.post(url, params={"organization_id": str(ORG)}, headers=headers)
        other = await http.post(url, params={"organization_id": str(uuid.uuid4())}, headers=headers)

    assert first.status_code == 202
    assert other.status_code == 422
    assert other.json()["type"].endswith("/idempotency-key-reused")
    assert "Idempotent-Replayed" not in other.headers
    assert len(jobs.audits) == 1


async def test_retry__no_idempotency_store_wired__503_and_not_retried() -> None:
    """Without the store the route cannot keep its once-per-key promise, so it refuses (503
    not-ready), with a key or without one."""
    jobs = dead()
    async with client(Role.ADMIN, jobs=jobs, replays=False) as http:
        keyed = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")
        http.event_hooks["request"] = []
        bare = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")

    assert [keyed.status_code, bare.status_code] == [503, 503]
    assert keyed.json()["type"].endswith("/not-ready")
    assert jobs.audits == []


async def test_retry__without_an_idempotency_key__the_staged_rule() -> None:
    """Required once STAGED_KEY_REQUIRED flips (400, nothing changed); until then it runs."""
    jobs = dead()
    async with client(Role.ADMIN, jobs=jobs) as http:
        http.event_hooks["request"] = []
        answer = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")

    if STAGED_KEY_REQUIRED:
        assert answer.status_code == 400
        assert answer.json()["type"].endswith("/idempotency-key-required")
        assert jobs.audits == []
    else:
        assert answer.status_code == 202
        assert len(jobs.audits) == 1


async def test_retry__an_execution_job__409_naming_the_execution() -> None:
    async with client(jobs=dead(EXECUTION_JOB_KIND)) as http:
        answer = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")

    assert answer.status_code == 409
    body = answer.json()
    assert body["type"].endswith("/retry-through-execution")
    assert body["execution_id"] == str(EXECUTION)


async def test_retry__not_dead__409() -> None:
    async with client(jobs=dead(state="queued")) as http:
        answer = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")

    assert answer.status_code == 409
    assert answer.json()["type"].endswith("/job-not-dead")


async def test_retry__unknown__404() -> None:
    async with client(jobs=Jobs()) as http:
        answer = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")

    assert answer.status_code == 404


@pytest.mark.ac("AC-6.4.3")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_email_failures__the_admin_view_with_a_masked_recipient(role: Role) -> None:
    emails = MemoryFailedEmails([failure(1), failure(2)])
    async with client(role, emails=emails) as http:
        answer = await http.get("/v1/operations/email-failures", params={"limit": 1})

    assert answer.status_code == 200, answer.text
    body = answer.json()
    [item] = body["items"]
    assert item == {
        "id": str(failure(2).id),
        "kind": "incident_opened",
        "status": "failed",
        "reason": "resend 422",
        "created_at": failure(2).created_at.isoformat().replace("+00:00", "Z"),
        "recipient_user_id": str(MEMBER),
        "recipient": "o***@e***.test",
        "shop_id": str(SHOP),
        "incident_id": str(INCIDENT),
        "receipt_id": 7,
    }
    assert body["next_cursor"] is not None
    assert answer.headers["Cache-Control"] == "no-store"
    async with client(role, emails=emails) as http:
        rest = await http.get(
            "/v1/operations/email-failures", params={"cursor": body["next_cursor"]}
        )
    assert [i["id"] for i in rest.json()["items"]] == [str(failure(1).id)]


async def test_email_failures__skipped_on_request() -> None:
    emails = MemoryFailedEmails()
    async with client(emails=emails) as http:
        answer = await http.get("/v1/operations/email-failures", params={"status": "skipped"})

    assert answer.status_code == 200
    assert emails.asked[0][1] == "skipped"


@pytest.mark.parametrize("params", [{"status": "sent"}, {"limit": 0}, {"cursor": "nope"}])
async def test_email_failures__invalid__422(params: dict[str, str | int]) -> None:
    async with client() as http:
        answer = await http.get("/v1/operations/email-failures", params=params)

    assert answer.status_code == 422


@pytest.mark.parametrize("role", [Role.DEVELOPER, Role.OPERATIONS, Role.SUPPORT, Role.VIEWER])
async def test_email_failures__other_roles__403(role: Role) -> None:
    emails = MemoryFailedEmails([failure(1)])
    async with client(role, emails=emails) as http:
        answer = await http.get("/v1/operations/email-failures")

    assert answer.status_code == 403
    assert emails.asked == []


async def test_email_failures__another_organization__404() -> None:
    emails = MemoryFailedEmails([failure(1)])
    async with client(emails=emails) as http:
        answer = await http.get(
            "/v1/operations/email-failures", params={"organization_id": str(uuid.uuid4())}
        )

    assert answer.status_code == 404
    assert emails.asked == []


async def test_email_failures__not_wired__503() -> None:
    async with client(wired=False) as http:
        answer = await http.get("/v1/operations/email-failures")

    assert answer.status_code == 503


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.VIEWER])
async def test_retry__other_roles__403_and_nothing_changed(role: Role) -> None:
    jobs = dead()
    async with client(role, jobs=jobs) as http:
        answer = await http.post(f"/v1/operations/dead-letters/{JOB}/retry")

    assert answer.status_code == 403
    assert jobs.audits == []
