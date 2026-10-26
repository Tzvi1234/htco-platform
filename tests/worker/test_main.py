"""The worker process (slice 5.1): settings, the scheduled jobs' feature flag, the handlers'
wiring, the health answer, and the GET-only Etsy path of the daily probe."""

import base64
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from htco_application.audit import AuditRecord
from htco_application.jobs import DEFAULT_RETRY, ClaimedJob
from htco_application.kernel import TenantShop
from htco_application.maintenance import EXPIRY_JOB, PROBE_JOB, PURGE_JOB, SCHEDULES
from htco_application.messaging import OUTBOX_DELIVERY_JOB
from htco_application.shop_capabilities import CapabilityChecks
from htco_application.shop_integrations import EtsyConnection, OAuthTokens
from htco_domain.jobs import JobState
from htco_integrations.etsy import EtsyConfig
from htco_platform.settings import ConfigurationError, load_settings
from htco_worker.health import health_answer
from htco_worker.main import (
    EXIT_CONFIGURATION_ERROR,
    main,
    maintenance_handlers,
    outbox_handlers,
)
from htco_worker.settings import WorkerSettings
from tests.application.test_messaging import Store
from tests.etsy_fakes import ETSY_SHOP, NOW, Capabilities, Pinning, Vault
from tests.integrations.test_etsy_adapter import Harness, ok
from tests.job_fakes import MemoryJobQueue

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "etsy"
ORG = uuid.UUID("f0000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("f3000000-0000-0000-0000-000000000001")
VAULT_KEY = "k1:" + base64.b64encode(os.urandom(32)).decode()
SCOPES = frozenset({"shops_r", "transactions_r", "listings_r"})


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_ENVIRONMENT", "test")
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    return monkeypatch


@dataclass
class Directory:
    connected: list[TenantShop] = field(default_factory=list)

    def connected_shops(self) -> list[TenantShop]:
        return list(self.connected)

    def purgeable_shops(self, *, now: datetime) -> list[TenantShop]:  # noqa: ARG002 - the port
        return []


class NoPurges:
    async def purge(
        self,
        shop: TenantShop,  # noqa: ARG002 - the port's signature
        *,
        now: datetime,  # noqa: ARG002
        audit: AuditRecord,  # noqa: ARG002
    ) -> dict[str, int] | None:
        return None


# --- settings ------------------------------------------------------------------------------


@pytest.mark.usefixtures("environment")
def test_settings__by_default__no_schedule_runs() -> None:
    settings = load_settings(WorkerSettings)

    assert settings.schedules == ()
    assert settings.worker_id
    assert settings.worker_health_port == 8081


def test_settings__unknown_schedule__is_refused(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("HTCO_WORKER_SCHEDULES", "maintenance.purge_disconnected_shops,nope.nope")

    with pytest.raises(ConfigurationError, match="unknown schedules"):
        load_settings(WorkerSettings)


def test_settings__probes_without_the_vault_key__are_refused(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_WORKER_SCHEDULES", "maintenance.capability_probes")

    with pytest.raises(ConfigurationError, match="HTCO_TOKEN_VAULT_KEY"):
        load_settings(WorkerSettings)


def test_settings__probes_without_the_etsy_app__are_refused(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_WORKER_SCHEDULES", "maintenance.capability_probes")
    environment.setenv("HTCO_TOKEN_VAULT_KEY", VAULT_KEY)

    with pytest.raises(ConfigurationError, match="HTCO_ETSY_KEYSTRING"):
        load_settings(WorkerSettings)


def test_settings__invalid_vault_key__is_refused_without_repeating_it(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_TOKEN_VAULT_KEY", "not-a-key-zzz")

    with pytest.raises(ConfigurationError) as refused:
        load_settings(WorkerSettings)

    assert "not-a-key-zzz" not in str(refused.value)


def test_settings__every_schedule_with_its_configuration__is_accepted(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_WORKER_SCHEDULES", ", ".join(SCHEDULES))
    environment.setenv("HTCO_TOKEN_VAULT_KEY", VAULT_KEY)
    environment.setenv("HTCO_ETSY_KEYSTRING", "fake-keystring")
    environment.setenv("HTCO_ETSY_SHARED_SECRET", "fake-secret")

    settings = load_settings(WorkerSettings)

    assert settings.schedules == tuple(SCHEDULES)
    assert settings.key_ring is not None


def test_main__invalid_configuration__exits_78(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.delenv("HTCO_DATABASE_URL")

    assert main() == EXIT_CONFIGURATION_ERROR
    assert "HTCO_DATABASE_URL" in capsys.readouterr().err


@pytest.mark.usefixtures("environment")
def test_main__valid_configuration__serves_with_the_settings() -> None:
    served: list[WorkerSettings] = []

    async def serve(settings: WorkerSettings) -> None:
        served.append(settings)

    assert main(serve=serve) == 0
    [settings] = served
    assert settings.environment == "test"


# --- handlers -----------------------------------------------------------------------------


def test_maintenance_handlers__nothing_enabled__no_handler() -> None:
    handlers = maintenance_handlers(
        (), queue=MemoryJobQueue(), directory=Directory(), vault=None, checks=None, purge=NoPurges()
    )

    assert handlers == {}


def test_maintenance_handlers__every_schedule__its_fan_out_and_its_shop_job() -> None:
    vault = Vault()
    checks = CapabilityChecks(
        vault=vault, marketplace=_unused(), store=Capabilities(), tokens=Pinning()
    )

    handlers = maintenance_handlers(
        tuple(SCHEDULES),
        queue=MemoryJobQueue(),
        directory=Directory(),
        vault=vault,
        checks=checks,
        purge=NoPurges(),
    )

    assert set(handlers) == set(SCHEDULES) | {PROBE_JOB, EXPIRY_JOB, PURGE_JOB}


def _unused() -> Any:
    return object()


def _job(kind: str, **changes: Any) -> ClaimedJob:
    values: dict[str, Any] = {
        "id": uuid.uuid7(),
        "kind": kind,
        "idempotency_key": f"{kind}:{SHOP}:2026-10-22",
        "payload": {},
        "organization_id": ORG,
        "shop_id": SHOP,
        "lock_key": f"shop:{SHOP}:etsy",
        "attempts": 1,
        "retry": DEFAULT_RETRY,
        "lease_expires_at": NOW + timedelta(minutes=1),
    }
    values.update(changes)
    return ClaimedJob(**values)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.ac("AC-4.5.2")
async def test_daily_probe_job__reaches_etsy_with_gets_only() -> None:
    requests: list[httpx.Request] = []
    answers = [
        ok(_fixture("shop.json")),
        ok(_fixture("receipts_page.json")),
        ok(_fixture("listings_page.json")),
    ]

    def strict_etsy(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "GET":
            pytest.fail(f"a {request.method} request reached Etsy: {request.url.path}")
        return answers.pop(0)

    harness = Harness(strict_etsy, EtsyConfig())
    vault = Vault()
    vault.add(
        EtsyConnection(
            organization_id=ORG,
            shop_id=SHOP,
            etsy_shop_id=ETSY_SHOP,
            etsy_user_id=12345678,
            scopes=SCOPES,
            tokens=OAuthTokens(
                access_token="12345678.fake-access-token",  # noqa: S106 - fake
                refresh_token="12345678.fake-refresh-token",  # noqa: S106 - fake
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=90),
            ),
        )
    )
    store = Capabilities()
    checks = CapabilityChecks(
        vault=vault, marketplace=harness.adapter, store=store, tokens=Pinning()
    )
    handlers = maintenance_handlers(
        ("maintenance.capability_probes",),
        queue=MemoryJobQueue(),
        directory=Directory(),
        vault=None,
        checks=checks,
        purge=NoPurges(),
    )

    async with harness.client:
        await handlers[PROBE_JOB](_job(PROBE_JOB))

    assert len(requests) == 3
    assert {r.method for r in requests} == {"GET"}
    assert len(store.saved) == 1


async def test_fan_out_job__enqueues_one_probe_per_connected_shop() -> None:
    queue = MemoryJobQueue()
    handlers = maintenance_handlers(
        ("maintenance.capability_probes",),
        queue=queue,
        directory=Directory(connected=[TenantShop(ORG, SHOP)]),
        vault=None,
        checks=CapabilityChecks(
            vault=Vault(), marketplace=_unused(), store=Capabilities(), tokens=Pinning()
        ),
        purge=NoPurges(),
    )

    await handlers["maintenance.capability_probes"](_job("maintenance.capability_probes"))

    [job] = queue.of_kind(PROBE_JOB)
    assert (job.spec.organization_id, job.spec.shop_id, job.state) == (ORG, SHOP, JobState.QUEUED)


async def test_shop_job_without_a_shop__fails() -> None:
    vault = Vault()
    handlers = maintenance_handlers(
        ("maintenance.refresh_token_expiry",),
        queue=MemoryJobQueue(),
        directory=Directory(),
        vault=vault,
        checks=None,
        purge=NoPurges(),
    )

    with pytest.raises(ValueError, match="needs a shop"):
        await handlers[EXPIRY_JOB](_job(EXPIRY_JOB, shop_id=None))


# --- health -------------------------------------------------------------------------------


def test_health_answer__alive__200_ok() -> None:
    answer = health_answer(b"GET /healthz HTTP/1.1", alive=True)

    assert answer.startswith(b"HTTP/1.1 200 OK\r\n")
    assert answer.endswith(b"\r\n\r\nok\n")


def test_health_answer__stalled__503() -> None:
    assert health_answer(b"GET /healthz HTTP/1.1", alive=False).startswith(b"HTTP/1.1 503")


@pytest.mark.parametrize("line", [b"", b"POST /healthz HTTP/1.1", b"GET / HTTP/1.1", b"junk"])
def test_health_answer__anything_else__404(line: bytes) -> None:
    assert health_answer(line, alive=True).startswith(b"HTTP/1.1 404")


def test_settings__a_worker_id_longer_than_the_queue_columns__is_refused(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_WORKER_ID", "w" * 201)

    with pytest.raises(ConfigurationError, match="HTCO_WORKER_ID"):
        load_settings(WorkerSettings)


def test_outbox_handlers__deliver_the_outbox_job() -> None:
    assert set(outbox_handlers(Store(), {})) == {OUTBOX_DELIVERY_JOB}
