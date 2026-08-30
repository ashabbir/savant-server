import pytest

from postgres_client import require_test_database


pytestmark = pytest.mark.no_db


class FakeCursor:
    def __init__(self, database_name):
        self.database_name = database_name

    def execute(self, statement):
        assert statement == "SELECT current_database() AS database_name"

    def fetchone(self):
        return {"database_name": self.database_name}


def test_require_test_database_rejects_production_database():
    with pytest.raises(RuntimeError, match="Refusing to run destructive test setup"):
        require_test_database(FakeCursor("savant"))


def test_require_test_database_accepts_test_database():
    require_test_database(FakeCursor("savant_test"))
