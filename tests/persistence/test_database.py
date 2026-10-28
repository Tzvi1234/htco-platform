"""The connection pool wrapper (slice 2.3); its Postgres behavior is in tests/integration."""

from psycopg.conninfo import conninfo_to_dict

from htco_persistence.database import Database


def test_database__repr__hides_the_connection_string() -> None:
    database = Database("postgresql://htco:secret-password@db.invalid/htco")
    assert "secret-password" not in repr(database)
    assert repr(database) == "Database(<conninfo hidden>)"


def test_database__construction__does_not_connect() -> None:
    Database("postgresql://htco@127.0.0.1:1/htco")  # would fail if it opened the pool


def test_database__a_password_apart__joins_the_login_without_url_encoding() -> None:
    """The worker's own login (architecture audit 5.1): the URL carries no password; the one
    given apart reaches libpq unchanged, even with characters a URL would need escaped."""
    password = "fake p@ss/word:%?"  # noqa: S105 - an invented password
    database = Database("postgresql://htco_worker_login@db.invalid:5432/htco", password=password)

    parts = conninfo_to_dict(database._conninfo)
    assert parts["user"] == "htco_worker_login"
    assert parts["password"] == password
    assert parts["host"] == "db.invalid"
    assert parts["dbname"] == "htco"
    assert password not in repr(database)


def test_database__no_password_apart__keeps_the_url() -> None:
    url = "postgresql://htco@db.invalid/htco"
    assert Database(url)._conninfo == url
