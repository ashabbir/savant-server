"""Regression tests for the pytest PostgreSQL destructive-operation guard."""

import pytest

from test_database import TestDatabaseConfigurationError, require_test_database_url

pytestmark = pytest.mark.no_db


def test_fixture_refuses_before_any_destructive_database_work(monkeypatch, tmp_path):
    """The autouse fixture must fail before importing or truncating PostgreSQL."""
    from types import SimpleNamespace
    from tests.conftest import _isolated_db

    monkeypatch.delenv("SAVANT_TEST_DATABASE_URL", raising=False)
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda _name: None))

    fixture = _isolated_db.__wrapped__(tmp_path, monkeypatch, request)
    with pytest.raises(TestDatabaseConfigurationError, match="Refusing destructive test setup"):
        next(fixture)


def test_requires_an_explicit_test_database_url(monkeypatch):
    monkeypatch.delenv("SAVANT_TEST_DATABASE_URL", raising=False)

    with pytest.raises(TestDatabaseConfigurationError, match="SAVANT_TEST_DATABASE_URL"):
        require_test_database_url()


@pytest.mark.parametrize("url", [
    "postgresql://user:password@db.example:5432/savant",
    "postgresql://user:password@db.example:5432/postgres",
])
def test_refuses_urls_that_do_not_name_a_test_database(monkeypatch, url):
    monkeypatch.setenv("SAVANT_TEST_DATABASE_URL", url)

    with pytest.raises(TestDatabaseConfigurationError, match="database name containing 'test'"):
        require_test_database_url()


def test_allows_a_dedicated_test_database(monkeypatch):
    url = "postgresql://user:password@db.example:5432/savant_test"
    monkeypatch.setenv("SAVANT_TEST_DATABASE_URL", url)

    assert require_test_database_url() == url
