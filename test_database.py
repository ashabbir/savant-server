"""Safety checks for test databases that receive destructive setup SQL."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


class TestDatabaseConfigurationError(RuntimeError):
    """Raised when pytest has not been pointed at an isolated database."""

    __test__ = False


def require_test_database_url() -> str:
    """Return the explicitly configured PostgreSQL test URL or fail closed.

    The pytest fixture truncates tables before every test.  Requiring a
    dedicated URL whose database name includes ``test`` makes an accidental
    fallback to the development or production ``savant`` database impossible.
    """
    url = os.environ.get("SAVANT_TEST_DATABASE_URL", "").strip()
    if not url:
        raise TestDatabaseConfigurationError(
            "Refusing destructive test setup: set SAVANT_TEST_DATABASE_URL "
            "to a dedicated PostgreSQL test database."
        )

    database_name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    if "test" not in database_name:
        raise TestDatabaseConfigurationError(
            "Refusing destructive test setup: SAVANT_TEST_DATABASE_URL must "
            "use a database name containing 'test'."
        )
    return url
