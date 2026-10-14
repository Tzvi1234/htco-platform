"""The connection pool wrapper (slice 2.3); its Postgres behavior is in tests/integration."""

from htco_persistence.database import Database


def test_database__repr__hides_the_connection_string() -> None:
    database = Database("postgresql://htco:secret-password@db.invalid/htco")
    assert "secret-password" not in repr(database)
    assert repr(database) == "Database(<conninfo hidden>)"


def test_database__construction__does_not_connect() -> None:
    Database("postgresql://htco@127.0.0.1:1/htco")  # would fail if it opened the pool
