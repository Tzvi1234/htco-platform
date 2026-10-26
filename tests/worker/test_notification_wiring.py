"""The worker's notification wiring (slice 6.4): the event subscribers, the email destination,
the log adapter by default and Resend only when configured with its key."""

import httpx
import pytest

from htco_application.alerts import OUT_OF_STOCK_EVENT
from htco_application.incidents import INCIDENT_ESCALATED, INCIDENT_OPENED, INCIDENT_RESOLVED
from htco_application.notifications import EMAIL_DESTINATION, NOTIFICATION_REQUESTED, EventDeliverer
from htco_integrations.email import LogEmailAdapter, ResendEmailAdapter
from htco_platform.settings import ConfigurationError, load_settings
from htco_worker.main import email_port, notification_deliverers
from htco_worker.settings import WorkerSettings
from tests.worker.test_main import environment  # noqa: F401 - the fixture


class NoDatabase:
    """Deliverers are built, never run, in these tests."""


@pytest.mark.usefixtures("environment")
def test_events_and_email_are_delivered_by_default_with_the_log_adapter() -> None:
    settings = load_settings(WorkerSettings)
    deliverers = notification_deliverers(settings, NoDatabase(), httpx.AsyncClient())  # type: ignore[arg-type]
    events = deliverers["events.publish"]
    assert isinstance(events, EventDeliverer)
    assert set(events.subscribers) == {
        INCIDENT_ESCALATED,
        INCIDENT_OPENED,
        INCIDENT_RESOLVED,
        NOTIFICATION_REQUESTED,
        OUT_OF_STOCK_EVENT,
    }
    assert EMAIL_DESTINATION in deliverers
    assert isinstance(email_port(settings, httpx.AsyncClient()), LogEmailAdapter)


def test_resend_needs_its_key(environment: pytest.MonkeyPatch) -> None:  # noqa: F811
    environment.setenv("HTCO_EMAIL_PROVIDER", "resend")
    with pytest.raises(ConfigurationError, match="RESEND_API_KEY"):
        load_settings(WorkerSettings)
    environment.setenv("HTCO_RESEND_API_KEY", "re_fake_for_tests")
    settings = load_settings(WorkerSettings)
    assert isinstance(email_port(settings, httpx.AsyncClient()), ResendEmailAdapter)
