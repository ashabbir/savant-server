from postgres_client import (
    _DATA_MIGRATIONS,
    _SCHEMA_MIGRATIONS,
    _backfill_kg_node_creators,
    _execute_schema_sql,
    _reconcile_additive_schema,
    _run_pending_migrations,
)

ALL_VERSIONS = tuple(
    {v for v, _, _ in _SCHEMA_MIGRATIONS} | {v for v, _, _ in _DATA_MIGRATIONS}
)


class FakeCursor:
    def __init__(self, applied_versions=(), fetchone_results=()):
        self.applied_versions = applied_versions
        self.fetchone_results = list(fetchone_results)
        self.executed = []
        self.rowcount = 0

    def execute(self, statement, params=None):
        self.executed.append((statement, params))

    def fetchall(self):
        return [{"version": version} for version in self.applied_versions]

    def fetchone(self):
        return self.fetchone_results.pop(0) if self.fetchone_results else None


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
    assert any("CREATE TABLE IF NOT EXISTS app_variables" in sql for sql in statements)
    assert not any("CREATE TABLE IF NOT EXISTS tool_packages" in sql for sql in statements)


def test_fresh_schema_has_kg_node_creator_column():
    cursor = FakeCursor()

    _execute_schema_sql(cursor)

    kg_nodes_ddl = next(sql for sql, _ in cursor.executed if "CREATE TABLE IF NOT EXISTS kg_nodes" in sql)
    assert "created_by" in kg_nodes_ddl


def test_applied_migrations_are_skipped():
    cursor = FakeCursor(applied_versions=ALL_VERSIONS)

    applied = _run_pending_migrations(cursor)

    assert applied == []
    assert not any("ALTER TABLE" in sql for sql, _ in cursor.executed)
    assert not any("UPDATE kg_nodes" in sql for sql, _ in cursor.executed)


def test_existing_deployments_receive_pending_schema_migrations():
    cursor = FakeCursor(applied_versions=(1, 2, 3, 4, 6))

    applied = _run_pending_migrations(cursor)

    assert applied == [5, 7, 8, 9, 10, 11, 8, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
    assert any("DROP CONSTRAINT IF EXISTS kg_nodes_node_type_check" in sql for sql, _ in cursor.executed)
    assert any("ADD CONSTRAINT kg_nodes_node_type_check" in sql for sql, _ in cursor.executed)
    assert any("idx_notebook_memberships_user" in sql for sql, _ in cursor.executed)
    assert any("reject_engram_immutable_update" in sql for sql, _ in cursor.executed)
    assert any("notebook_artifact_renditions" in sql for sql, _ in cursor.executed)
    assert any(params == (5, "enforce knowledge graph node types") for _, params in cursor.executed)
    assert any(params == (15, "create app_variables key-value store") for _, params in cursor.executed)
    assert any("DROP TABLE IF EXISTS tool_packages" in sql for sql, _ in cursor.executed)


def test_schema_reconciliation_repairs_drift_even_after_migrations_are_stamped():
    cursor = FakeCursor(applied_versions=(1,))

    _reconcile_additive_schema(cursor)

    assert any("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active" in sql for sql, _ in cursor.executed)


# ── KG node creator: migration 20 (schema) + 21 (one-time backfill) ─────────


def test_creator_column_added_before_backfill_runs():
    cursor = FakeCursor(
        applied_versions=tuple(v for v in ALL_VERSIONS if v not in (20, 21)),
        fetchone_results=[{"pending": True}, {"user_id": "ahmed"}],
    )

    applied = _run_pending_migrations(cursor)

    assert applied == [20, 21]
    statements = [sql for sql, _ in cursor.executed]
    add_col = next(i for i, sql in enumerate(statements) if "ADD COLUMN IF NOT EXISTS created_by" in sql)
    backfill = next(i for i, sql in enumerate(statements) if "UPDATE kg_nodes SET created_by" in sql)
    assert add_col < backfill


def test_backfill_assigns_unattributed_nodes_to_first_admin():
    cursor = FakeCursor(fetchone_results=[{"pending": True}, {"user_id": "ahmed"}])

    assert _backfill_kg_node_creators(cursor) is True

    sql, params = cursor.executed[-1]
    assert "UPDATE kg_nodes SET created_by = %s WHERE created_by = ''" in sql
    assert params == ("ahmed",)
    admin_sql = cursor.executed[1][0]
    assert "role = 'admin'" in admin_sql
    assert "LIKE 'ahmed%'" in admin_sql
    assert "created_at ASC" in admin_sql


def test_backfill_is_noop_on_fresh_instance_and_still_recorded():
    cursor = FakeCursor(fetchone_results=[{"pending": False}])

    assert _backfill_kg_node_creators(cursor) is True
    assert not any("UPDATE kg_nodes" in sql for sql, _ in cursor.executed)


def test_backfill_deferred_when_no_admin_exists():
    cursor = FakeCursor(
        applied_versions=tuple(v for v in ALL_VERSIONS if v != 21),
        fetchone_results=[{"pending": True}, None],
    )

    applied = _run_pending_migrations(cursor)

    assert applied == []
    assert not any("UPDATE kg_nodes" in sql for sql, _ in cursor.executed)
    assert not any(params and params[0] == 21 for _, params in cursor.executed)


def test_backfill_is_never_replayed_by_reconciliation():
    cursor = FakeCursor(applied_versions=ALL_VERSIONS)

    _reconcile_additive_schema(cursor)

    assert any("ADD COLUMN IF NOT EXISTS created_by" in sql for sql, _ in cursor.executed)
    assert not any("UPDATE kg_nodes SET created_by" in sql for sql, _ in cursor.executed)
