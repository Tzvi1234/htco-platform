"""Where the database tests find Postgres: the local Supabase stack (`just db-start`) or the CI
service, through `HTCO_TEST_DATABASE_URL`."""

import os
from urllib.parse import urlsplit

DATABASE_URL_VARIABLE = "HTCO_TEST_DATABASE_URL"
# CI sets this so that a missing database fails the run instead of silently running fewer tests.
REQUIRE_VARIABLE = "HTCO_REQUIRE_DATABASE_TESTS"


def database_url() -> str:
    url = os.environ.get(DATABASE_URL_VARIABLE)
    if not url:
        raise RuntimeError(
            f"{DATABASE_URL_VARIABLE} is not set; start the stack with `just db-start`"
        )
    return url


def parse_host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.hostname is None or parts.port is None:
        raise ValueError("the database URL needs an explicit host and port")
    return parts.hostname, parts.port
