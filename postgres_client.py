"""
PostgreSQL client and connection pool management for Savant.

Replaces sqlite_client.py. Uses psycopg2 with a ThreadedConnectionPool
so each Flask thread gets a real pooled connection rather than a per-thread
SQLite file handle.

Connection URL:  SAVANT_DATABASE_URL  (required in production)
Default fallback: postgresql://savant_user:savant_secure_password@localhost:5432/savant
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Generator

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.getenv(
    "SAVANT_DATABASE_URL",
    "postgresql://savant_user:savant_secure_password@localhost:5432/savant",
)

_POOL: ThreadedConnectionPool | None = None
_POOL_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------


def _make_pool() -> ThreadedConnectionPool:
    """Create a new connection pool."""
    logger.info("Initialising PostgreSQL connection pool → %s", DATABASE_URL.split("@")[-1])
    pool = ThreadedConnectionPool(
        minconn=5,
        maxconn=50,
        dsn=DATABASE_URL,
    )
    return pool


def _get_pool() -> ThreadedConnectionPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = _make_pool()
    return _POOL


def get_connection() -> psycopg2.extensions.connection:
    """Borrow a connection from the pool.

    IMPORTANT: callers must call release_connection() when done, or use
    the db_cursor() context manager which handles this automatically.
    """
    conn = _get_pool().getconn()
    # Ensure dict-like row access (same ergonomics as sqlite3.Row)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def release_connection(conn: psycopg2.extensions.connection) -> None:
    """Return a connection to the pool."""
    if _POOL is not None:
        _POOL.putconn(conn)


@contextmanager
def db_cursor() -> Generator[psycopg2.extensions.cursor, None, None]:
    """Context manager that yields a RealDictCursor and auto-commits/rollbacks.

    Usage::

        with db_cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (uid,))
            row = cur.fetchone()
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def close_pool() -> None:
    """Gracefully close all pool connections (call on app shutdown)."""
    global _POOL
    if _POOL is not None:
        _POOL.closeall()
        _POOL = None
        logger.info("PostgreSQL connection pool closed.")


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Users
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT DEFAULT '',
    api_key         TEXT NOT NULL UNIQUE,
    api_key_hash    TEXT NOT NULL UNIQUE,
    role            TEXT DEFAULT 'user',
    is_active       INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_api_key_hash ON users(api_key_hash);

-- Workspaces
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id        TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT DEFAULT '',
    priority            TEXT DEFAULT 'medium',
    status              TEXT DEFAULT 'open',
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    created_session_id  TEXT,
    user_id             TEXT DEFAULT '',
    color               TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ws_status ON workspaces(status);
CREATE INDEX IF NOT EXISTS idx_ws_created ON workspaces(created_at);

-- Workspace <-> Session links
CREATE TABLE IF NOT EXISTS workspace_session_links (
    workspace_id        TEXT NOT NULL,
    provider            TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    attached_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider, session_id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_wsl_workspace ON workspace_session_links(workspace_id);
CREATE INDEX IF NOT EXISTS idx_wsl_attached ON workspace_session_links(attached_at DESC);

-- Tasks
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    seq                 INTEGER UNIQUE,
    workspace_id        TEXT NOT NULL,
    title               TEXT NOT NULL,
    description         TEXT DEFAULT '',
    status              TEXT DEFAULT 'todo',
    priority            TEXT DEFAULT 'medium',
    date                TEXT,
    "order"             INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    created_session_id  TEXT,
    user_id             TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_ws_status ON tasks(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_date_order ON tasks(date, "order");
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

-- Task dependencies
CREATE TABLE IF NOT EXISTS task_deps (
    task_id     TEXT NOT NULL,
    depends_on  TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on),
    FOREIGN KEY (task_id)    REFERENCES tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES tasks(task_id) ON DELETE CASCADE
);

-- Auto-increment counter for task seq
CREATE TABLE IF NOT EXISTS counters (
    name    TEXT PRIMARY KEY,
    value   INTEGER NOT NULL DEFAULT 0
);
INSERT INTO counters (name, value) VALUES ('task_seq', 0)
    ON CONFLICT (name) DO NOTHING;

-- Notes
CREATE TABLE IF NOT EXISTS notes (
    note_id         TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    workspace_id    TEXT,
    text            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    user_id         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_workspace ON notes(workspace_id);

-- Merge Requests
CREATE TABLE IF NOT EXISTS merge_requests (
    mr_id           TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    project_id      TEXT DEFAULT '',
    mr_iid          INTEGER DEFAULT 0,
    title           TEXT DEFAULT '',
    status          TEXT DEFAULT 'open',
    priority        TEXT DEFAULT 'medium',
    author          TEXT DEFAULT '',
    jira            TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    user_id         TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_mr_workspace ON merge_requests(workspace_id);
CREATE INDEX IF NOT EXISTS idx_mr_status ON merge_requests(status);
CREATE INDEX IF NOT EXISTS idx_mr_created ON merge_requests(created_at);

-- MR notes
CREATE TABLE IF NOT EXISTS mr_notes (
    id          SERIAL PRIMARY KEY,
    mr_id       TEXT NOT NULL,
    session_id  TEXT DEFAULT '',
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (mr_id) REFERENCES merge_requests(mr_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mr_notes_mr ON mr_notes(mr_id, created_at);

-- MR <-> Session assignments
CREATE TABLE IF NOT EXISTS mr_sessions (
    mr_id       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    role        TEXT DEFAULT 'author',
    assigned_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (mr_id, session_id),
    FOREIGN KEY (mr_id) REFERENCES merge_requests(mr_id) ON DELETE CASCADE
);

-- Jira Tickets
CREATE TABLE IF NOT EXISTS jira_tickets (
    ticket_id       TEXT PRIMARY KEY,
    workspace_id    TEXT DEFAULT '',
    ticket_key      TEXT NOT NULL UNIQUE,
    title           TEXT DEFAULT '',
    status          TEXT DEFAULT 'todo',
    priority        TEXT DEFAULT 'medium',
    assignee        TEXT DEFAULT '',
    reporter        TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    user_id         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jira_workspace ON jira_tickets(workspace_id);
CREATE INDEX IF NOT EXISTS idx_jira_status ON jira_tickets(status);
CREATE INDEX IF NOT EXISTS idx_jira_key ON jira_tickets(ticket_key);
CREATE INDEX IF NOT EXISTS idx_jira_created ON jira_tickets(created_at);

-- Jira notes
CREATE TABLE IF NOT EXISTS jira_notes (
    id          SERIAL PRIMARY KEY,
    ticket_id   TEXT NOT NULL,
    session_id  TEXT DEFAULT '',
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES jira_tickets(ticket_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jira_notes_ticket ON jira_notes(ticket_id, created_at);

-- Jira <-> Session assignments
CREATE TABLE IF NOT EXISTS jira_sessions (
    ticket_id   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    role        TEXT DEFAULT 'assignee',
    assigned_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ticket_id, session_id),
    FOREIGN KEY (ticket_id) REFERENCES jira_tickets(ticket_id) ON DELETE CASCADE
);

-- Notifications
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    message         TEXT NOT NULL,
    detail          TEXT DEFAULT '{}',
    workspace_id    TEXT,
    session_id      TEXT,
    read            INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL,
    user_id         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notif_created   ON notifications(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notif_read      ON notifications(read);
CREATE INDEX IF NOT EXISTS idx_notif_workspace ON notifications(workspace_id);
CREATE INDEX IF NOT EXISTS idx_notif_session   ON notifications(session_id);

-- Preferences
CREATE TABLE IF NOT EXISTS preferences (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Meta key-value store
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Experiences
CREATE TABLE IF NOT EXISTS experiences (
    experience_id   TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'note',
    workspace_id    TEXT DEFAULT '',
    repo            TEXT DEFAULT '',
    files           TEXT DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    user_id         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_exp_workspace ON experiences(workspace_id);
CREATE INDEX IF NOT EXISTS idx_exp_source    ON experiences(source);
CREATE INDEX IF NOT EXISTS idx_exp_created   ON experiences(created_at DESC);

-- Knowledge Graph: Nodes
CREATE TABLE IF NOT EXISTS kg_nodes (
    node_id     TEXT PRIMARY KEY,
    node_type   TEXT NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT DEFAULT '',
    metadata    TEXT DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'staged'
);
CREATE INDEX IF NOT EXISTS idx_kgn_type    ON kg_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_kgn_created ON kg_nodes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kgn_title   ON kg_nodes(title);

-- Knowledge Graph: Edges
CREATE TABLE IF NOT EXISTS kg_edges (
    edge_id     TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES kg_nodes(node_id) ON DELETE CASCADE,
    target_id   TEXT NOT NULL REFERENCES kg_nodes(node_id) ON DELETE CASCADE,
    edge_type   TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    label       TEXT DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kge_source ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_kge_target ON kg_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_kge_type   ON kg_edges(edge_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kge_unique ON kg_edges(source_id, target_id, edge_type);

-- Graphify: Nodes
CREATE TABLE IF NOT EXISTS graphify_nodes (
    node_id      TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES ctx_repos(name) ON DELETE CASCADE,
    node_type    TEXT NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT DEFAULT '',
    metadata     TEXT DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_gfn_ws      ON graphify_nodes(workspace_id);
CREATE INDEX IF NOT EXISTS idx_gfn_type    ON graphify_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_gfn_title   ON graphify_nodes(title);
CREATE INDEX IF NOT EXISTS idx_gfn_created ON graphify_nodes(created_at DESC);

-- Graphify: Edges
CREATE TABLE IF NOT EXISTS graphify_edges (
    edge_id      TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES ctx_repos(name) ON DELETE CASCADE,
    source_id    TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    edge_type    TEXT NOT NULL,
    weight       REAL DEFAULT 1.0,
    label        TEXT DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (workspace_id, source_id) REFERENCES graphify_nodes(workspace_id, node_id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, target_id) REFERENCES graphify_nodes(workspace_id, node_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gfe_ws     ON graphify_edges(workspace_id);
CREATE INDEX IF NOT EXISTS idx_gfe_source ON graphify_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_gfe_target ON graphify_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_gfe_type   ON graphify_edges(edge_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gfe_unique ON graphify_edges(workspace_id, source_id, target_id, edge_type);

-- Job queue
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    job_type    TEXT NOT NULL,
    target      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    INTEGER DEFAULT 0,
    phase       TEXT DEFAULT '',
    message     TEXT DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    result      TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_target  ON jobs(target, job_type);

-- Reminders
CREATE TABLE IF NOT EXISTS reminders (
    reminder_id         TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    description         TEXT DEFAULT '',
    priority            TEXT DEFAULT 'medium',
    status              TEXT DEFAULT 'pending',
    start_date          TEXT NOT NULL,
    due_date            TEXT NOT NULL,
    remind_before_hrs   INTEGER DEFAULT 1,
    notified            INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    user_id             TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reminders_status  ON reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_due     ON reminders(due_date);
CREATE INDEX IF NOT EXISTS idx_reminders_created ON reminders(created_at DESC);

-- -------------------------------------------------------------------------
-- Context / Embedding tables (replaces sqlite-vec)
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ctx_repos (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    status      TEXT DEFAULT 'added',
    indexed_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ctx_files (
    id              SERIAL PRIMARY KEY,
    repo_id         INTEGER NOT NULL REFERENCES ctx_repos(id) ON DELETE CASCADE,
    rel_path        TEXT NOT NULL,
    language        TEXT,
    is_memory_bank  INTEGER DEFAULT 0,
    mtime_ns        BIGINT,
    indexed_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(repo_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_ctx_files_repo ON ctx_files(repo_id);
CREATE INDEX IF NOT EXISTS idx_ctx_files_lang ON ctx_files(language);
CREATE INDEX IF NOT EXISTS idx_ctx_files_mb   ON ctx_files(is_memory_bank, repo_id);

CREATE TABLE IF NOT EXISTS ctx_chunks (
    id          SERIAL PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES ctx_files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ctx_chunks_file ON ctx_chunks(file_id);

CREATE TABLE IF NOT EXISTS ctx_ast_nodes (
    id          SERIAL PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES ctx_files(id) ON DELETE CASCADE,
    node_type   TEXT NOT NULL,
    name        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ctx_ast_file ON ctx_ast_nodes(file_id);

-- pgvector embedding table (replaces sqlite-vec ctx_vec_chunks virtual table)
CREATE TABLE IF NOT EXISTS ctx_vec_chunks (
    chunk_id    INTEGER PRIMARY KEY REFERENCES ctx_chunks(id) ON DELETE CASCADE,
    embedding   vector(768) NOT NULL
);
-- HNSW index for fast approximate nearest-neighbour search (cosine distance)
CREATE INDEX IF NOT EXISTS idx_ctx_vec_hnsw
    ON ctx_vec_chunks USING hnsw (embedding vector_cosine_ops);

-- Schema upgrades/migrations for existing databases
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id);
"""


def init_schema() -> None:
    """Create all tables and indexes. Safe to call multiple times (IF NOT EXISTS)."""
    logger.info("Running schema initialisation…")
    with db_cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (0x5A0A17,))
        try:
            # Run migrations for graphify tables to support multi-repo isolation
            try:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'graphify_nodes'
                    ) as tbl_exists;
                """)
                exists_row = cur.fetchone()
                table_exists = exists_row["tbl_exists"] if isinstance(exists_row, dict) else exists_row[0]
                if table_exists:
                    cur.execute("""
                        SELECT COUNT(*) as cnt
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                         AND tc.table_schema = kcu.table_schema
                        WHERE tc.table_name = 'graphify_nodes'
                          AND tc.constraint_type = 'PRIMARY KEY';
                    """)
                    cnt_row = cur.fetchone()
                    pk_col_count = cnt_row["cnt"] if isinstance(cnt_row, dict) else cnt_row[0]
                    if pk_col_count == 1:
                        logger.info("Migrating graphify tables for multi-repo isolation...")
                        cur.execute("ALTER TABLE graphify_edges DROP CONSTRAINT IF EXISTS graphify_edges_source_id_fkey;")
                        cur.execute("ALTER TABLE graphify_edges DROP CONSTRAINT IF EXISTS graphify_edges_target_id_fkey;")
                        cur.execute("ALTER TABLE graphify_nodes DROP CONSTRAINT IF EXISTS graphify_nodes_pkey;")
                        cur.execute("ALTER TABLE graphify_nodes ADD CONSTRAINT graphify_nodes_pkey PRIMARY KEY (workspace_id, node_id);")
                        cur.execute("""
                            ALTER TABLE graphify_edges 
                            ADD CONSTRAINT graphify_edges_source_fk 
                            FOREIGN KEY (workspace_id, source_id) REFERENCES graphify_nodes(workspace_id, node_id) 
                            ON DELETE CASCADE;
                        """)
                        cur.execute("""
                            ALTER TABLE graphify_edges 
                            ADD CONSTRAINT graphify_edges_target_fk 
                            FOREIGN KEY (workspace_id, target_id) REFERENCES graphify_nodes(workspace_id, node_id) 
                            ON DELETE CASCADE;
                        """)
                        logger.info("Graphify table migration completed successfully.")
            except Exception as migrate_err:
                logger.warning(f"Graphify migration check skipped or failed: {migrate_err}")
                cur.connection.rollback()

            # Execute statement by statement to avoid psycopg2 multi-statement issues
            statements = [s.strip() for s in _SCHEMA_SQL.split(";") if s.strip()]
            for stmt in statements:
                try:
                    cur.execute(stmt)
                except Exception as e:
                    logger.error(f"Failed to execute SQL statement: {stmt}")
                    logger.error(f"Error detail: {e}")
                    raise
            cur.connection.commit()
        except Exception as e:
            cur.connection.rollback()
            raise
        finally:
            try:
                cur.execute("SELECT pg_advisory_unlock(%s)", (0x5A0A17,))
                cur.connection.commit()
            except Exception as lock_err:
                logger.error(f"Failed to release advisory lock: {lock_err}")
                try:
                    cur.connection.rollback()
                except Exception:
                    pass
    logger.info("Schema initialisation complete.")
