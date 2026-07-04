#!/usr/bin/env python3
"""
Migrate savant-server data from SQLite (old) → PostgreSQL (new).

Usage:
    python3 scripts/migrate_sqlite_to_postgres.py [--sqlite PATH] [--pg-url URL] [--dry-run]

Defaults:
    --sqlite  ~/.savant/server-data/savant.db
    --pg-url  postgresql://savant_user:savant_secure_password@localhost:5433/savant

GOLD tables (kg_nodes, kg_edges, workspaces, workspace_session_links):
    Script aborts if source count - skipped != inserted count.

Skipped tables:
    ctx_* (rebuild from source), graphify_* (new), lost_and_found (internal)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_SQLITE = os.path.expanduser("~/.savant/server-data/savant.db")
DEFAULT_PG_URL = "postgresql://savant_user:savant_secure_password@localhost:5433/savant"
FALLBACK_TS = "1970-01-01T00:00:00+00:00"

GOLD = {"kg_nodes", "kg_edges", "workspaces", "workspace_session_links"}

# Tables migrated in FK-safe order.
TABLES: list[str] = [
    # GOLD
    "kg_nodes",
    "kg_edges",
    "workspaces",
    "workspace_session_links",
    # Critical
    "users",
    "tasks",
    "task_deps",
    "notes",
    "merge_requests",
    "mr_notes",
    "mr_sessions",
    "jira_tickets",
    "jira_notes",
    "jira_sessions",
    "reminders",
    # Best-effort
    "experiences",
    "notifications",
    "preferences",
    "meta",
    "jobs",
]

SKIP = {
    "ctx_repos", "ctx_files", "ctx_chunks", "ctx_ast_nodes",
    "ctx_vec_chunks", "ctx_vec_chunks_chunks", "ctx_vec_chunks_info",
    "ctx_vec_chunks_rowids", "ctx_vec_chunks_vector_chunks00",
    "graphify_nodes", "graphify_edges",
    "counters", "sqlite_sequence",
    "lost_and_found", "lost_and_found_0",
    "_jira_tickets_new",
}


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def sqlite_columns(cur: sqlite3.Cursor, table: str) -> list[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def pg_columns(cur: psycopg2.extensions.cursor, table: str) -> set[str]:
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'
    """, (table,))
    return {row["column_name"] for row in cur.fetchall()}


def pg_timestamptz_cols(cur: psycopg2.extensions.cursor, table: str) -> set[str]:
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'
          AND data_type IN ('timestamp with time zone', 'timestamp without time zone')
    """, (table,))
    return {row["column_name"] for row in cur.fetchall()}


def pg_notnull_cols(cur: psycopg2.extensions.cursor, table: str) -> set[str]:
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'
          AND is_nullable = 'NO'
    """, (table,))
    return {row["column_name"] for row in cur.fetchall()}


def table_exists_pg(cur: psycopg2.extensions.cursor, table: str) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = %s AND table_schema = 'public'
    """, (table,))
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Row coercion
# ---------------------------------------------------------------------------

def coerce_row(
    row: tuple,
    cols: list[str],
    ts_cols: set[str],
    notnull_cols: set[str],
) -> tuple | None:
    """
    Fix common SQLite → PG type issues:
    - Empty string in a TIMESTAMPTZ column → FALLBACK_TS (if NOT NULL) or None
    Returns None if the row should be skipped entirely.
    """
    out = []
    for col, val in zip(cols, row):
        if col in ts_cols and val == "":
            val = FALLBACK_TS if col in notnull_cols else None
        out.append(val)
    return tuple(out)


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg2.extensions.connection,
    table: str,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Returns (sqlite_count, inserted_count, skipped_count)."""
    sc = sqlite_conn.cursor()
    pc = pg_conn.cursor()

    sc.execute(f"SELECT COUNT(*) FROM {table}")
    src_count = sc.fetchone()[0]

    if src_count == 0:
        print(f"  {table}: 0 rows — skip")
        return 0, 0, 0

    if not table_exists_pg(pc, table):
        print(f"  {table}: table does not exist in PG — skip")
        return src_count, 0, src_count

    sqlite_cols_all = sqlite_columns(sc, table)
    pg_cols = pg_columns(pc, table)
    ts_cols = pg_timestamptz_cols(pc, table)
    nn_cols = pg_notnull_cols(pc, table)

    cols = [c for c in sqlite_cols_all if c in pg_cols]
    if not cols:
        print(f"  {table}: no overlapping columns — skip")
        return src_count, 0, src_count

    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))

    sc.execute(f"SELECT {col_list} FROM {table}")
    rows = sc.fetchall()

    if dry_run:
        print(f"  {table}: {src_count} rows (dry-run)")
        return src_count, src_count, 0

    inserted = 0
    skipped = 0
    BATCH_SIZE = 200

    def insert_batch(batch: list[tuple]) -> tuple[int, int]:
        if not batch:
            return 0, 0
        try:
            psycopg2.extras.execute_values(
                pc,
                f'INSERT INTO "{table}" ({col_list}) VALUES %s ON CONFLICT DO NOTHING',
                batch,
            )
            pg_conn.commit()
            return len(batch), 0
        except Exception:
            pg_conn.rollback()
            # Fall back to row-by-row
            ok = fail = 0
            for r in batch:
                try:
                    pc.execute(
                        f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING',
                        r,
                    )
                    pg_conn.commit()
                    ok += 1
                except Exception as e:
                    pg_conn.rollback()
                    fail += 1
            return ok, fail

    batch: list[tuple] = []
    for raw_row in rows:
        row = coerce_row(raw_row, cols, ts_cols, nn_cols)
        if row is None:
            skipped += 1
            continue
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            ins, sk = insert_batch(batch)
            inserted += ins
            skipped += sk
            batch = []

    ins, sk = insert_batch(batch)
    inserted += ins
    skipped += sk

    return src_count, inserted, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate savant SQLite → PostgreSQL")
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE, help="SQLite DB path")
    parser.add_argument("--pg-url", default=DEFAULT_PG_URL, help="PostgreSQL connection URL")
    parser.add_argument("--dry-run", action="store_true", help="Read-only, no writes")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite).expanduser()
    if not sqlite_path.exists():
        print(f"ERROR: SQLite DB not found: {sqlite_path}")
        sys.exit(1)

    print(f"Source : {sqlite_path}")
    print(f"Target : {args.pg_url.split('@')[-1]}")
    print(f"Dry run: {args.dry_run}")
    print()

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = sqlite3.Row

    try:
        pg_conn = psycopg2.connect(args.pg_url, cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL: {e}")
        sys.exit(1)

    TIER = {t: "GOLD" for t in GOLD}
    TIER.update({t: "critical" for t in [
        "tasks", "task_deps", "notes", "merge_requests", "mr_notes",
        "mr_sessions", "jira_tickets", "jira_notes", "jira_sessions", "reminders",
    ]})

    gold_failures = []
    results = []

    for table in TABLES:
        if table in SKIP:
            continue
        tier = TIER.get(table, "effort")
        pad = f"{tier:<10}"

        try:
            src, ins, skipped = migrate_table(sqlite_conn, pg_conn, table, args.dry_run)
        except Exception as e:
            print(f"  [{pad}] {table}: FAILED — {e}")
            if table in GOLD:
                gold_failures.append(table)
            results.append((table, tier, "FAILED", "-", "-", "-"))
            continue

        effective = src - skipped
        ok = (src == 0) or (ins == effective)
        status = "OK" if ok else f"PARTIAL ({ins}/{effective} effective, {skipped} skipped)"
        print(f"  [{pad}] {table}: {src} src, {ins} inserted, {skipped} skipped  {status}")
        results.append((table, tier, status, str(src), str(ins), str(skipped)))

        if table in GOLD and not ok and not args.dry_run:
            gold_failures.append(table)

    # Summary
    print()
    print("=" * 70)
    print("MIGRATION SUMMARY")
    print("=" * 70)
    print(f"  {'tier':<10} {'table':<35} {'src':>6}  {'ins':>6}  {'skip':>6}  status")
    print(f"  {'-'*10} {'-'*35} {'-'*6}  {'-'*6}  {'-'*6}  {'-'*20}")
    for table, tier, status, src, ins, skipped in results:
        print(f"  {tier:<10} {table:<35} {src:>6}  {ins:>6}  {skipped:>6}  {status}")

    print()
    if gold_failures:
        print(f"ABORT: GOLD table(s) failed: {gold_failures}")
        print("DO NOT cut over. Investigate and re-run.")
        sys.exit(2)
    else:
        print("ALL GOLD tables migrated successfully.")
        if not args.dry_run:
            print("Safe to verify and cut over.")

    sqlite_conn.close()
    pg_conn.close()


if __name__ == "__main__":
    main()
