"""Persistence and resolution for application variables (key-value store)."""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from postgres_client import get_connection, release_connection

logger = logging.getLogger(__name__)


class AppVariablesDB:
    """Key-value persistence for app-level variables such as API tokens."""

    @staticmethod
    def get(key: str) -> Optional[str]:
        """Fetch variable value from the database.

        Returns None if key is not found or if database query fails.
        """
        conn = None
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_variables WHERE key = %s", (key,))
                row = cur.fetchone()
                if row:
                    val = row["value"] if isinstance(row, dict) else row[0]
                    return str(val) if val is not None else None
                return None
        except Exception as e:
            logger.debug("Failed to read '%s' from app_variables table: %s", key, e)
            return None
        finally:
            if conn is not None:
                try:
                    release_connection(conn)
                except Exception:
                    pass

    @staticmethod
    def set(key: str, value: str) -> None:
        """Upsert variable key-value into the database."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO app_variables (key, value)
                       VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                    (key, str(value)),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def delete(key: str) -> bool:
        """Delete a variable by key from the database.

        Returns True if a row was deleted, False otherwise.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_variables WHERE key = %s", (key,))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        finally:
            release_connection(conn)

    @staticmethod
    def list_all() -> Dict[str, str]:
        """List all key-value pairs stored in app_variables."""
        conn = None
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM app_variables ORDER BY key")
                rows = cur.fetchall()
                result: Dict[str, str] = {}
                for row in rows:
                    k = row["key"] if isinstance(row, dict) else row[0]
                    v = row["value"] if isinstance(row, dict) else row[1]
                    result[str(k)] = str(v)
                return result
        except Exception as e:
            logger.debug("Failed to list app_variables: %s", e)
            return {}
        finally:
            if conn is not None:
                try:
                    release_connection(conn)
                except Exception:
                    pass

    @staticmethod
    def get_effective_variable(key: str) -> str:
        """Retrieve variable value, preferring DB over environment variable.

        Resolution logic:
        1. Check database app_variables table.
           If a non-empty string is found in DB, return it.
        2. Fall back to environment variable (os.environ.get(key, '')).
        3. If neither provides a non-empty string, return empty string "".
        """
        db_val = AppVariablesDB.get(key)
        if db_val is not None and db_val.strip():
            return db_val.strip()
        return os.environ.get(key, "").strip()

    @staticmethod
    def get_effective_info(key: str) -> dict:
        """Return metadata about where the effective variable is sourced from.

        Returns dict with:
        - key: the variable key
        - is_set: bool
        - source: 'db' | 'env' | 'none'
        - db_configured: bool
        - env_configured: bool
        """
        db_val = AppVariablesDB.get(key)
        db_has = bool(db_val and db_val.strip())
        env_val = os.environ.get(key, "").strip()
        env_has = bool(env_val)

        if db_has:
            source = "db"
        elif env_has:
            source = "env"
        else:
            source = "none"

        return {
            "key": key,
            "is_set": db_has or env_has,
            "source": source,
            "db_configured": db_has,
            "env_configured": env_has,
        }
