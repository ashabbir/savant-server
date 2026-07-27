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

-- Server-owned tool registry.  Archives and metadata live in PostgreSQL so a
-- tool remains available after container replacement or a renderer refresh.
CREATE TABLE IF NOT EXISTS tool_packages (
    name            TEXT PRIMARY KEY,
    description     TEXT NOT NULL DEFAULT '',
    input_schema    JSONB NOT NULL DEFAULT '{"type":"object","properties":{}}'::jsonb,
    archive_data    BYTEA NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    uploaded_by     TEXT NOT NULL REFERENCES users(user_id),
    service_node_id TEXT NOT NULL DEFAULT '',
    kg_node_ids     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_packages_created ON tool_packages(created_at DESC);

-- User <-> Domain Node Assignments
CREATE TABLE IF NOT EXISTS user_domains (
    user_id         TEXT NOT NULL,
    domain_node_id  TEXT NOT NULL,
    can_write       INTEGER DEFAULT 1,
    assigned_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, domain_node_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ud_user ON user_domains(user_id);
CREATE INDEX IF NOT EXISTS idx_ud_domain ON user_domains(domain_node_id);

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
    colosseum_ready     BOOLEAN NOT NULL DEFAULT FALSE,
    colosseum_config    JSONB NOT NULL DEFAULT '{}'::jsonb,
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

-- Task ended days
CREATE TABLE IF NOT EXISTS task_ended_days (
    date        TEXT PRIMARY KEY,
    user_id     TEXT DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT NOW()
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

-- Applied database migrations
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    node_type   TEXT NOT NULL CONSTRAINT kg_nodes_node_type_check CHECK (
        node_type IN ('insight', 'client', 'domain', 'service', 'library',
                      'technology', 'project', 'concept', 'repo', 'session',
                      'issue', 'person', 'operation', 'organization')
    ),
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

-- -------------------------------------------------------------------------
-- Context / Embedding tables (replaces sqlite-vec)
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ctx_repos (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    status      TEXT DEFAULT 'added',
    indexed_at  TIMESTAMPTZ,
    last_fetched_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

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

-- Provider-neutral structural code intelligence operational metadata.
-- Graph nodes/edges remain owned by the selected provider and are never mirrored here.
CREATE TABLE IF NOT EXISTS code_intelligence_config (
    repo_id          INTEGER PRIMARY KEY REFERENCES ctx_repos(id) ON DELETE CASCADE,
    provider         TEXT NOT NULL DEFAULT 'codegraph'
                     CHECK (provider = 'codegraph'),
    index_root       TEXT,
    engine_version   TEXT,
    graph_version    TEXT,
    last_indexed_at  TIMESTAMPTZ,
    last_synced_at   TIMESTAMPTZ,
    freshness        TEXT NOT NULL DEFAULT 'unavailable'
                     CHECK (freshness IN ('fresh', 'pending_sync', 'stale', 'degraded', 'unavailable')),
    last_error_code  TEXT,
    last_error_at    TIMESTAMPTZ,
    watch_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    rollout_state    TEXT NOT NULL DEFAULT 'codegraph_primary'
                     CHECK (rollout_state = 'codegraph_primary'),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ci_config_provider ON code_intelligence_config(provider);

-- At most one queued/running structural writer job may exist per repository.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_structural_repo
ON jobs(target)
WHERE status IN ('queued', 'running')
  AND job_type IN ('codegraph_index', 'codegraph_sync', 'codegraph_delete');

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

CREATE TABLE IF NOT EXISTS ctx_repo_sync_logs (
    id           SERIAL PRIMARY KEY,
    repo_name    TEXT NOT NULL,
    operation    TEXT NOT NULL DEFAULT 'periodic_refresh',
    trigger      TEXT NOT NULL DEFAULT 'scheduled',
    provider     TEXT DEFAULT '',
    branch       TEXT DEFAULT '',
    actor_id     TEXT DEFAULT '',
    source_app   TEXT DEFAULT '',
    status       TEXT NOT NULL,
    before_commit TEXT DEFAULT '',
    after_commit TEXT DEFAULT '',
    fetched      BOOLEAN DEFAULT FALSE,
    code_changed BOOLEAN DEFAULT FALSE,
    indexed      BOOLEAN DEFAULT FALSE,
    graphed      BOOLEAN DEFAULT FALSE,
    duration_ms  BIGINT DEFAULT 0,
    error        TEXT DEFAULT '',
    commit_subject TEXT DEFAULT '',
    files_changed JSONB NOT NULL DEFAULT '{"added":[],"modified":[],"deleted":[]}'::jsonb,
    change_stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    legacy_periodic_log_id INTEGER UNIQUE,
    details      TEXT DEFAULT '',
    created_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ctx_repo_sync_logs_repo ON ctx_repo_sync_logs(repo_name);
CREATE INDEX IF NOT EXISTS idx_ctx_repo_sync_logs_created ON ctx_repo_sync_logs(created_at DESC);

"""

_SCHEMA_MIGRATIONS = (
    (
        1,
        "reconcile additive columns and indexes",
        (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active INTEGER DEFAULT 1",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT ''",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS color TEXT DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS colosseum_ready BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS colosseum_config JSONB NOT NULL DEFAULT '{}'::jsonb",
            "CREATE INDEX IF NOT EXISTS idx_tasks_colosseum_ready ON tasks(workspace_id, colosseum_ready, status)",
            "ALTER TABLE notes ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT ''",
            "ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT ''",
            "ALTER TABLE jira_tickets ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT ''",
            "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT ''",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_workspaces_user ON workspaces(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_merge_requests_user ON merge_requests(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_jira_tickets_user ON jira_tickets(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id)",
        ),
    ),
    (
        2,
        "track repository fetch timestamps",
        (
            "ALTER TABLE ctx_repos ADD COLUMN IF NOT EXISTS last_fetched_at TIMESTAMPTZ",
        ),
    ),
    (
        3,
        "remove legacy graphify storage and select codegraph",
        (
            "UPDATE code_intelligence_config SET provider = 'codegraph', rollout_state = 'codegraph_primary'",
            "ALTER TABLE code_intelligence_config ALTER COLUMN provider SET DEFAULT 'codegraph'",
            "ALTER TABLE code_intelligence_config ALTER COLUMN rollout_state SET DEFAULT 'codegraph_primary'",
            "ALTER TABLE code_intelligence_config DROP CONSTRAINT IF EXISTS code_intelligence_config_provider_check",
            "ALTER TABLE code_intelligence_config ADD CONSTRAINT code_intelligence_config_provider_check CHECK (provider = 'codegraph')",
            "ALTER TABLE code_intelligence_config DROP CONSTRAINT IF EXISTS code_intelligence_config_rollout_state_check",
            "ALTER TABLE code_intelligence_config ADD CONSTRAINT code_intelligence_config_rollout_state_check CHECK (rollout_state = 'codegraph_primary')",
            "DROP TABLE IF EXISTS graphify_edges",
            "DROP TABLE IF EXISTS graphify_nodes",
        ),
    ),
    (
        4,
        "generalize repository sync activity logs",
        (
            """CREATE TABLE IF NOT EXISTS ctx_periodic_sync_logs (
                   id SERIAL PRIMARY KEY, repo_name TEXT NOT NULL, status TEXT NOT NULL,
                   fetched BOOLEAN DEFAULT FALSE, code_changed BOOLEAN DEFAULT FALSE,
                   indexed BOOLEAN DEFAULT FALSE, graphed BOOLEAN DEFAULT FALSE,
                   details TEXT DEFAULT '', created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
               )""",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS operation TEXT NOT NULL DEFAULT 'periodic_refresh'",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS trigger TEXT NOT NULL DEFAULT 'scheduled'",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS provider TEXT DEFAULT ''",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS branch TEXT DEFAULT ''",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS actor_id TEXT DEFAULT ''",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS source_app TEXT DEFAULT ''",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS before_commit TEXT DEFAULT ''",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS after_commit TEXT DEFAULT ''",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS duration_ms BIGINT DEFAULT 0",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS error TEXT DEFAULT ''",
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS legacy_periodic_log_id INTEGER UNIQUE",
            """INSERT INTO ctx_repo_sync_logs
                   (repo_name, operation, trigger, status, fetched, code_changed,
                    indexed, graphed, details, created_at, legacy_periodic_log_id)
               SELECT repo_name, 'periodic_refresh', 'scheduled', status, fetched,
                      code_changed, indexed, graphed, details, created_at, id
               FROM ctx_periodic_sync_logs
               ON CONFLICT (legacy_periodic_log_id) DO NOTHING""",
            "CREATE INDEX IF NOT EXISTS idx_ctx_repo_sync_logs_repo ON ctx_repo_sync_logs(repo_name)",
            "CREATE INDEX IF NOT EXISTS idx_ctx_repo_sync_logs_created ON ctx_repo_sync_logs(created_at DESC)",
        ),
    ),
    (
        5,
        "enforce knowledge graph node types",
        (
            "ALTER TABLE kg_nodes DROP CONSTRAINT IF EXISTS kg_nodes_node_type_check",
            """ALTER TABLE kg_nodes ADD CONSTRAINT kg_nodes_node_type_check CHECK (
                   node_type IN ('insight', 'client', 'domain', 'service', 'library',
                                 'technology', 'project', 'concept', 'repo', 'session',
                                 'issue', 'person', 'operation', 'organization')
               )""",
        ),
    ),
    (
        6,
        "add repository activity change details",
        (
            "ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS commit_subject TEXT DEFAULT ''",
            """ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS files_changed
               JSONB NOT NULL DEFAULT '{"added":[],"modified":[],"deleted":[]}'::jsonb""",
            """ALTER TABLE ctx_repo_sync_logs ADD COLUMN IF NOT EXISTS change_stats
               JSONB NOT NULL DEFAULT '{}'::jsonb""",
            "CREATE INDEX IF NOT EXISTS idx_ctx_repo_sync_logs_operation ON ctx_repo_sync_logs(operation)",
            "CREATE INDEX IF NOT EXISTS idx_ctx_repo_sync_logs_trigger ON ctx_repo_sync_logs(trigger)",
        ),
    ),
)


def _execute_schema_sql(cur: psycopg2.extensions.cursor) -> None:
    """Create any missing current-schema tables and indexes."""
    statements = [statement.strip() for statement in _SCHEMA_SQL.split(";") if statement.strip()]
    for statement in statements:
        cur.execute(statement)


def _run_pending_migrations(cur: psycopg2.extensions.cursor) -> list[int]:
    """Apply and record every migration not yet present in schema_migrations."""
    cur.execute("SELECT version FROM schema_migrations ORDER BY version")
    rows = cur.fetchall()
    applied_versions = {
        row["version"] if isinstance(row, dict) else row[0]
        for row in rows
    }
    applied = []

    for version, name, statements in _SCHEMA_MIGRATIONS:
        if version in applied_versions:
            continue
        logger.info("Applying database migration %s: %s", version, name)
        for statement in statements:
            cur.execute(statement)
        cur.execute(
            """
            INSERT INTO schema_migrations (version, name)
            VALUES (%s, %s)
            ON CONFLICT (version) DO NOTHING
            """,
            (version, name),
        )
        applied.append(version)

    return applied


def _reconcile_additive_schema(cur: psycopg2.extensions.cursor) -> None:
    """Repair known additive schema drift even when migrations are already stamped."""
    for _, _, statements in _SCHEMA_MIGRATIONS:
        for statement in statements:
            cur.execute(statement)


def init_schema() -> None:
    """Create missing schema objects and apply pending migrations."""
    logger.info("Checking database schema and pending migrations")
    conn = get_connection()
    lock_acquired = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (0x5A0A17,))
            lock_acquired = True
            _execute_schema_sql(cur)
            _run_pending_migrations(cur)
            _reconcile_additive_schema(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Database schema initialization failed")
        raise
    finally:
        if lock_acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (0x5A0A17,))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("Failed to release database schema advisory lock")
        release_connection(conn)
    logger.info("Database schema is current")
