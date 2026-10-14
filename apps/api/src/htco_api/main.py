"""Process entry point and composition root: `python -m htco_api`."""

import sys
from collections.abc import Callable

import structlog
import uvicorn
from fastapi import FastAPI

from htco_api.app import create_app
from htco_api.settings import ApiSettings
from htco_platform.logs import configure_logging
from htco_platform.settings import ConfigurationError, load_settings

# sysexits.h EX_CONFIG: the process cannot start because its configuration is wrong.
EXIT_CONFIGURATION_ERROR = 78


def _serve(app: FastAPI, settings: ApiSettings) -> None:
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # logging is already configured as JSON
        access_log=False,  # the request-context middleware writes the access log
        server_header=False,  # do not advertise the server software (ASVS 13.4.6)
    )


def main(*, serve: Callable[[FastAPI, ApiSettings], None] = _serve) -> int:
    """Load and validate settings, configure logging, then serve. Bad settings exit with 78."""
    try:
        settings = load_settings(ApiSettings)
    except ConfigurationError as error:
        sys.stderr.write(f"htco-api: invalid configuration\n{error}\n")
        return EXIT_CONFIGURATION_ERROR

    configure_logging(level=settings.log_level)
    structlog.get_logger(__name__).info(
        "api.starting",
        environment=settings.environment.value,
        host=settings.host,
        port=settings.port,
    )
    serve(create_app(), settings)
    return 0
