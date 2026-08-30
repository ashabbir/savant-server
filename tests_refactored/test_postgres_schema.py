from postgres_client import (
    _execute_schema_sql,
    _reconcile_additive_schema,
    _run_pending_migrations,
)


class FakeCursor:
    def __init__(self, applied_versions=()):
        self.applied_versions = applied_versions
        self.executed = []

    def execute(self, statement, params=None):
        self.executed.append((statement, params))

    def fetchall(self):
        return [{"version": version} for version in self.applied_versions]


def test_pending_migrations_apply_and_record_versions():
    cursor = FakeCursor()

    applied = _run_pending_migrations(cursor)

    assert applied
    assert any("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active" in sql for sql, _ in cursor.executed)
    assert any("FROM ctx_periodic_sync_logs" in sql for sql, _ in cursor.executed)
    assert any("legacy_periodic_log_id" in sql for sql, _ in cursor.executed)
    assert any("kg_nodes_node_type_check" in sql for sql, _ in cursor.executed)
    assert any("kg_maintenance_runs" in sql for sql, _ in cursor.executed)
    assert any("'operation'" in sql and "'organization'" in sql for sql, _ in cursor.executed)
    assert any("INSERT INTO schema_migrations" in sql for sql, _ in cursor.executed)


def test_current_postgres_schema_initializes_notebook_tables():
    cursor = FakeCursor()

    _execute_schema_sql(cursor)

    statements = [sql for sql, _ in cursor.executed]
    assert any("CREATE TABLE IF NOT EXISTS notebooks" in sql for sql in statements)
    assert any("CREATE TABLE IF NOT EXISTS notebook_memberships" in sql for sql in statements)
    assert any("CREATE TABLE IF NOT EXISTS notebook_artifact_versions" in sql for sql in statements)
    assert any("CREATE TABLE IF NOT EXISTS notebook_artifact_renditions" in sql for sql in statements)
    assert any("CREATE TABLE IF NOT EXISTS engram_items" in sql for sql in statements)
    assert any("CREATE TABLE IF NOT EXISTS engram_snapshots" in sql for sql in statements)
    assert any("CREATE TABLE IF NOT EXISTS conversation_compactions" in sql for sql in statements)


def test_applied_migrations_are_skipped():
    cursor = FakeCursor(applied_versions=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11))

    applied = _run_pending_migrations(cursor)

    assert applied == []
    assert not any("ALTER TABLE" in sql for sql, _ in cursor.executed)


def test_existing_deployments_receive_pending_schema_migrations():
    cursor = FakeCursor(applied_versions=(1, 2, 3, 4, 6))

    applied = _run_pending_migrations(cursor)

    assert applied == [5, 7, 8, 9, 10, 11]
    assert any("DROP CONSTRAINT IF EXISTS kg_nodes_node_type_check" in sql for sql, _ in cursor.executed)
    assert any("ADD CONSTRAINT kg_nodes_node_type_check" in sql for sql, _ in cursor.executed)
    assert any("idx_notebook_memberships_user" in sql for sql, _ in cursor.executed)
    assert any("reject_engram_immutable_update" in sql for sql, _ in cursor.executed)
    assert any("notebook_artifact_renditions" in sql for sql, _ in cursor.executed)
    assert any(params == (5, "enforce knowledge graph node types") for _, params in cursor.executed)


def test_schema_reconciliation_repairs_drift_even_after_migrations_are_stamped():
    cursor = FakeCursor(applied_versions=(1,))

    _reconcile_additive_schema(cursor)

    assert any("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active" in sql for sql, _ in cursor.executed)
