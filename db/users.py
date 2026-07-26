"""UserDB — PostgreSQL backend for user management and API key auth."""

import hashlib
import secrets
from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


def _hash_key(api_key: str) -> str:
    """SHA-256 hash of an API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


class UserDB:

    @staticmethod
    def _get_by_id_with_conn(user_id: str, conn) -> dict | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        return _row_to_dict(row)

    @staticmethod
    def create(user: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            api_key = user.get("api_key") or secrets.token_urlsafe(32)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO users
                       (user_id, name, email, api_key, api_key_hash, role, is_active, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                         name = EXCLUDED.name,
                         email = EXCLUDED.email,
                         api_key = EXCLUDED.api_key,
                         api_key_hash = EXCLUDED.api_key_hash,
                         role = EXCLUDED.role,
                         is_active = EXCLUDED.is_active,
                         updated_at = EXCLUDED.updated_at""",
                    (
                        user["user_id"],
                        user.get("name", ""),
                        user.get("email", ""),
                        api_key,
                        _hash_key(api_key),
                        user.get("role", "user"),
                        int(user.get("is_active", 1)),
                        user.get("created_at", now),
                        user.get("updated_at", now),
                    ),
                )
            conn.commit()
            return UserDB._get_by_id_with_conn(user["user_id"], conn)
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(user_id: str) -> dict | None:
        conn = get_connection()
        try:
            return UserDB._get_by_id_with_conn(user_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_api_key(api_key: str) -> dict | None:
        """Look up user by raw API key (hashed for comparison)."""
        conn = get_connection()
        try:
            key_hash = _hash_key(api_key)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE api_key_hash = %s", (key_hash,))
                row = cur.fetchone()
            return _row_to_dict(row)
        finally:
            release_connection(conn)

    @staticmethod
    def get_default_admin() -> dict | None:
        """Return the first admin user (dev fallback when no API key provided)."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY created_at ASC LIMIT 1")
                row = cur.fetchone()
            return _row_to_dict(row)
        finally:
            release_connection(conn)

    @staticmethod
    def list_all(include_inactive: bool = True) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if include_inactive:
                    cur.execute("SELECT * FROM users ORDER BY created_at ASC")
                else:
                    cur.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY created_at ASC")
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def update(user_id: str, updates: dict) -> dict | None:
        conn = get_connection()
        try:
            if isinstance(updates.get("is_active"), bool):
                updates["is_active"] = int(updates["is_active"])
            updates["updated_at"] = _now()
            valid_cols = {"name", "email", "role", "is_active", "updated_at"}
            filtered = {k: v for k, v in updates.items() if k in valid_cols}
            if not filtered:
                return UserDB._get_by_id_with_conn(user_id, conn)
            set_clause = ", ".join(f"{k} = %s" for k in filtered)
            values = list(filtered.values()) + [user_id]
            with conn.cursor() as cur:
                cur.execute(f"UPDATE users SET {set_clause} WHERE user_id = %s", values)
            conn.commit()
            return UserDB._get_by_id_with_conn(user_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def deactivate(user_id: str) -> dict | None:
        return UserDB.update(user_id, {"is_active": 0})

    @staticmethod
    def rotate_api_key(user_id: str) -> dict | None:
        conn = get_connection()
        try:
            api_key = secrets.token_urlsafe(32)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET api_key = %s, api_key_hash = %s, updated_at = %s WHERE user_id = %s",
                    (api_key, _hash_key(api_key), _now(), user_id),
                )
            conn.commit()
            return UserDB._get_by_id_with_conn(user_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def delete(user_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def seed_defaults() -> list[dict]:
        """Ensure default users exist. Returns the default user records."""
        conn = get_connection()
        try:
            defaults = [
                {
                    "user_id": "ahmed",
                    "name": "Ahmed Shabbir",
                    "email": "shabbir10314@gmail.com",
                    "api_key": "sk-ahmed-savant-001",
                    "role": "admin",
                    "is_active": 1,
                },
                {
                    "user_id": "lex",
                    "name": "Lex",
                    "email": "lex@savant.dev",
                    "api_key": "sk-lex-savant-001",
                    "role": "operator",
                    "is_active": 1,
                },
            ]
            now = _now()
            with conn.cursor() as cur:
                for user in defaults:
                    api_key = user["api_key"]
                    cur.execute(
                        """INSERT INTO users
                           (user_id, name, email, api_key, api_key_hash, role, is_active, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (user_id) DO UPDATE SET
                             name = EXCLUDED.name,
                             email = EXCLUDED.email,
                             api_key = EXCLUDED.api_key,
                             api_key_hash = EXCLUDED.api_key_hash,
                             role = EXCLUDED.role,
                             is_active = EXCLUDED.is_active,
                             updated_at = EXCLUDED.updated_at""",
                        (
                            user["user_id"],
                            user["name"],
                            user["email"],
                            api_key,
                            _hash_key(api_key),
                            user["role"],
                            int(user["is_active"]),
                            user.get("created_at", now),
                            now,
                        ),
                    )
            conn.commit()
            seeded = []
            for u in defaults:
                seeded.append(UserDB._get_by_id_with_conn(u["user_id"], conn))
            return seeded
        finally:
            release_connection(conn)

    @staticmethod
    def get_assigned_domains(user_id: str) -> list[dict]:
        """Return all assigned domain nodes for a user with write permission flags."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ud.user_id, ud.domain_node_id, ud.can_write, ud.assigned_at,
                              kn.title AS domain_title, kn.node_type
                       FROM user_domains ud
                       LEFT JOIN kg_nodes kn ON kn.node_id = ud.domain_node_id
                       WHERE ud.user_id = %s
                       ORDER BY ud.assigned_at ASC""",
                    (user_id,),
                )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def get_assigned_domain_write_map(user_id: str) -> dict[str, bool]:
        """Return dict mapping domain_node_id -> bool (can_write)."""
        assigned = UserDB.get_assigned_domains(user_id)
        return {item["domain_node_id"]: bool(item.get("can_write", 1)) for item in assigned}

    @staticmethod
    def assign_domain(user_id: str, domain_node_id: str, can_write: bool = True) -> dict:
        """Assign or update a domain node assignment for a user."""
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO user_domains (user_id, domain_node_id, can_write, assigned_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, domain_node_id) DO UPDATE SET
                         can_write = EXCLUDED.can_write""",
                    (user_id, domain_node_id, 1 if can_write else 0, now),
                )
            conn.commit()
            return {"user_id": user_id, "domain_node_id": domain_node_id, "can_write": bool(can_write)}
        finally:
            release_connection(conn)

    @staticmethod
    def remove_domain(user_id: str, domain_node_id: str) -> bool:
        """Unassign a domain node from a user."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_domains WHERE user_id = %s AND domain_node_id = %s",
                    (user_id, domain_node_id),
                )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)
