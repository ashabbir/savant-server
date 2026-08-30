"""PostgreSQL persistence for collaborative notebooks."""

import hashlib
import json
import uuid

from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


JSON_FIELDS = {"metadata": {}}
RENDITION_JSON_FIELDS = {"metadata": {}, "renderer_config": {}}
NOTEBOOK_JSON_FIELDS = {"cover_style": {}, "runtime_settings": {}}
SOURCE_JSON_FIELDS = {"metadata": {}, "provenance": {}}
NOTEBOOK_COLUMNS = """n.notebook_id, n.owner_user_id, n.title, n.description,
    n.objective, n.tagline, n.visibility, n.cover_media_type,
    n.cover_size_bytes, n.cover_hash, n.cover_style, n.runtime_settings,
    n.created_at, n.updated_at"""


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _row(row, json_fields=None):
    return _row_to_dict(row, json_fields=json_fields)


def _notebook_row(row):
    item = _row(row, NOTEBOOK_JSON_FIELDS)
    if not item:
        return None
    cover_content = item.pop("cover_content", None)
    item["cover"] = (
        {
            "url": f"/api/notebooks/{item['notebook_id']}/cover",
            "media_type": item.get("cover_media_type"),
            "size_bytes": item.get("cover_size_bytes"),
            "hash": item.get("cover_hash"),
            "style": item.get("cover_style") or {},
        }
        if cover_content is not None or item.get("cover_hash") or item.get("cover_style")
        else None
    )
    return item


def _source_row(row):
    item = _row(row, SOURCE_JSON_FIELDS)
    if item and item.get("source_type") in {"file", "directory"}:
        item["reference"] = ""
    return item


class NotebookMutationError(ValueError):
    pass


class NotebookDB:
    @staticmethod
    def create(data: dict, owner_user_id: str) -> dict:
        conn = get_connection()
        try:
            notebook_id = data.get("notebook_id") or _id("nb")
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebooks
                       (notebook_id, owner_user_id, title, description, objective,
                        tagline, visibility, cover_style, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        notebook_id,
                        owner_user_id,
                        data["title"],
                        data.get("description", ""),
                        data.get("objective", ""),
                        data.get("tagline", ""),
                        data.get("visibility", "private"),
                        json.dumps(data.get("cover_style") or {}),
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """INSERT INTO notebook_memberships
                       (notebook_id, user_id, role, granted_by, active,
                        active_at, revoked_at, updated_at)
                       VALUES (%s, %s, 'owner', %s, TRUE, %s, NULL, %s)""",
                    (notebook_id, owner_user_id, owner_user_id, now, now),
                )
            conn.commit()
            return NotebookDB.get_access(notebook_id, owner_user_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_for_user(
        user_id: str, limit: int = 100, cursor: str | None = None
    ) -> list[dict]:
        conn = get_connection()
        try:
            cursor_filter = ""
            params = [user_id, user_id, user_id]
            if cursor:
                cursor_filter = """AND (n.updated_at, n.notebook_id) <
                    (SELECT updated_at, notebook_id FROM notebooks
                     WHERE notebook_id = %s)"""
                params.append(cursor)
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT {NOTEBOOK_COLUMNS},
                              CASE WHEN n.owner_user_id = %s THEN 'owner' ELSE m.role END AS access_role
                       FROM notebooks n
                       LEFT JOIN notebook_memberships m
                         ON m.notebook_id = n.notebook_id
                        AND m.user_id = %s
                        AND m.active = TRUE
                        AND m.revoked_at IS NULL
                       WHERE (n.owner_user_id = %s OR m.user_id IS NOT NULL)
                         {cursor_filter}
                       ORDER BY n.updated_at DESC, n.notebook_id DESC
                       LIMIT %s""",
                    params,
                )
                return [_notebook_row(item) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def get_access(notebook_id: str, user_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT {NOTEBOOK_COLUMNS},
                              CASE WHEN n.owner_user_id = %s THEN 'owner' ELSE m.role END AS access_role
                       FROM notebooks n
                       LEFT JOIN notebook_memberships m
                         ON m.notebook_id = n.notebook_id
                        AND m.user_id = %s
                        AND m.active = TRUE
                        AND m.revoked_at IS NULL
                       WHERE n.notebook_id = %s
                         AND (n.owner_user_id = %s OR m.user_id IS NOT NULL)""",
                    (user_id, user_id, notebook_id, user_id),
                )
                return _notebook_row(cur.fetchone())
        finally:
            release_connection(conn)

    @staticmethod
    def update(notebook_id: str, updates: dict, user_id: str) -> dict | None:
        conn = get_connection()
        try:
            filtered = {
                key: value
                for key, value in updates.items()
                if key
                in {
                    "title",
                    "description",
                    "objective",
                    "visibility",
                    "tagline",
                    "cover_style",
                    "runtime_settings",
                }
                and value is not None
            }
            for json_field in ("cover_style", "runtime_settings"):
                if json_field in filtered:
                    filtered[json_field] = json.dumps(filtered[json_field])
            filtered["updated_at"] = _now()
            set_clause = ", ".join(f"{key} = %s" for key in filtered)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE notebooks SET {set_clause} WHERE notebook_id = %s",
                    [*filtered.values(), notebook_id],
                )
            conn.commit()
            return NotebookDB.get_access(notebook_id, user_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def delete(notebook_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM notebooks WHERE notebook_id = %s", (notebook_id,))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_members(notebook_id: str) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT m.*, u.name, u.email
                       FROM notebook_memberships m
                       JOIN users u ON u.user_id = m.user_id
                       WHERE m.notebook_id = %s
                       ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,
                                m.active_at""",
                    (notebook_id,),
                )
                return [_row(item) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def list_collaboration_users(
        owner_user_id: str,
        limit: int = 50,
        cursor: str | None = None,
        query: str = "",
    ) -> list[dict]:
        conn = get_connection()
        try:
            params = [owner_user_id]
            filters = ["u.is_active = 1", "u.user_id <> %s"]
            if cursor:
                filters.append("u.user_id > %s")
                params.append(cursor)
            if query:
                filters.append(
                    "(LOWER(u.name) LIKE %s OR LOWER(u.email) LIKE %s OR LOWER(u.user_id) LIKE %s)"
                )
                pattern = f"%{query.lower()}%"
                params.extend([pattern, pattern, pattern])
            params.append(limit + 1)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT u.user_id, u.name, u.email
                        FROM users u
                        WHERE {' AND '.join(filters)}
                        ORDER BY u.user_id
                        LIMIT %s""",
                    params,
                )
                return [_row(item) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def batch_assignments(owner_user_id: str, operations: list[dict]) -> list[dict]:
        conn = get_connection()
        try:
            now = _now()
            results = []
            with conn.cursor() as cur:
                notebook_ids = sorted({op["notebook_id"] for op in operations})
                cur.execute(
                    """SELECT notebook_id, owner_user_id
                       FROM notebooks
                       WHERE notebook_id = ANY(%s)
                       FOR UPDATE""",
                    (notebook_ids,),
                )
                notebooks = {row["notebook_id"]: row for row in cur.fetchall()}
                if len(notebooks) != len(notebook_ids):
                    raise NotebookMutationError("One or more notebooks were not found")
                if any(row["owner_user_id"] != owner_user_id for row in notebooks.values()):
                    raise NotebookMutationError("Only a notebook owner can change memberships")

                user_ids = sorted({op["user_id"] for op in operations})
                cur.execute(
                    """SELECT user_id, is_active FROM users
                       WHERE user_id = ANY(%s)""",
                    (user_ids,),
                )
                users = {row["user_id"]: row for row in cur.fetchall()}
                if len(users) != len(user_ids):
                    raise NotebookMutationError("One or more users were not found")
                upsert_user_ids = {
                    op["user_id"] for op in operations if op["action"] == "upsert"
                }
                if any(
                    user_id in upsert_user_ids and int(row["is_active"]) != 1
                    for user_id, row in users.items()
                ):
                    raise NotebookMutationError("Inactive users cannot be assigned")

                for operation in operations:
                    notebook_id = operation["notebook_id"]
                    user_id = operation["user_id"]
                    action = operation["action"]
                    role = operation.get("role")
                    notebook_owner = notebooks[notebook_id]["owner_user_id"]
                    if user_id == notebook_owner:
                        if action != "upsert" or role != "owner":
                            raise NotebookMutationError(
                                "Notebook owners cannot be demoted or removed"
                            )
                        results.append(
                            {
                                "notebook_id": notebook_id,
                                "user_id": user_id,
                                "role": "owner",
                                "active": True,
                            }
                        )
                        continue
                    if role == "owner":
                        raise NotebookMutationError(
                            "Ownership transfer is not supported by batch assignment"
                        )
                    if action == "remove":
                        cur.execute(
                            """UPDATE notebook_memberships
                               SET active = FALSE, revoked_at = %s, updated_at = %s
                               WHERE notebook_id = %s AND user_id = %s
                                 AND role <> 'owner' AND active = TRUE""",
                            (now, now, notebook_id, user_id),
                        )
                        results.append(
                            {
                                "notebook_id": notebook_id,
                                "user_id": user_id,
                                "active": False,
                                "removed": cur.rowcount > 0,
                            }
                        )
                    else:
                        cur.execute(
                            """INSERT INTO notebook_memberships
                               (notebook_id, user_id, role, granted_by, active,
                                active_at, revoked_at, updated_at)
                               VALUES (%s, %s, %s, %s, TRUE, %s, NULL, %s)
                               ON CONFLICT (notebook_id, user_id) DO UPDATE SET
                                 role = EXCLUDED.role,
                                 granted_by = EXCLUDED.granted_by,
                                 active = TRUE,
                                 active_at = EXCLUDED.active_at,
                                 revoked_at = NULL,
                                 updated_at = EXCLUDED.updated_at""",
                            (notebook_id, user_id, role, owner_user_id, now, now),
                        )
                        results.append(
                            {
                                "notebook_id": notebook_id,
                                "user_id": user_id,
                                "role": role,
                                "active": True,
                            }
                        )
            conn.commit()
            return results
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def get_member(notebook_id: str, user_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM notebook_memberships WHERE notebook_id = %s AND user_id = %s",
                    (notebook_id, user_id),
                )
                return _row(cur.fetchone())
        finally:
            release_connection(conn)

    @staticmethod
    def grant_member(notebook_id: str, user_id: str, role: str, granted_by: str) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebook_memberships
                       (notebook_id, user_id, role, granted_by, active,
                        active_at, revoked_at, updated_at)
                       VALUES (%s, %s, %s, %s, TRUE, %s, NULL, %s)
                       ON CONFLICT (notebook_id, user_id) DO UPDATE SET
                         role = EXCLUDED.role,
                         granted_by = EXCLUDED.granted_by,
                         active = TRUE,
                         active_at = EXCLUDED.active_at,
                         revoked_at = NULL,
                         updated_at = EXCLUDED.updated_at""",
                    (notebook_id, user_id, role, granted_by, now, now),
                )
            conn.commit()
            return NotebookDB.get_member(notebook_id, user_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def update_member(notebook_id: str, user_id: str, role: str, granted_by: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE notebook_memberships
                       SET role = %s, granted_by = %s, updated_at = %s
                       WHERE notebook_id = %s AND user_id = %s
                         AND active = TRUE AND revoked_at IS NULL""",
                    (role, granted_by, _now(), notebook_id, user_id),
                )
                updated = cur.rowcount > 0
            conn.commit()
            return NotebookDB.get_member(notebook_id, user_id) if updated else None
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def revoke_member(notebook_id: str, user_id: str) -> dict | None:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE notebook_memberships
                       SET active = FALSE, revoked_at = %s, updated_at = %s
                       WHERE notebook_id = %s AND user_id = %s
                         AND role <> 'owner' AND active = TRUE""",
                    (now, now, notebook_id, user_id),
                )
                updated = cur.rowcount > 0
            conn.commit()
            return NotebookDB.get_member(notebook_id, user_id) if updated else None
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def create_source(notebook_id: str, data: dict, user_id: str) -> dict:
        conn = get_connection()
        try:
            source_id = _id("src")
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebook_sources
                       (source_id, notebook_id, source_type, name, reference,
                        extracted_text, content_snapshot, media_type, size_bytes,
                        content_hash, content_version, provenance, status, metadata,
                        created_by, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s)""",
                    (
                        source_id,
                        notebook_id,
                        data["source_type"],
                        data.get("name", ""),
                        data.get("reference", ""),
                        data.get("extracted_text", ""),
                        data.get("content_snapshot", ""),
                        data.get("media_type", "text/plain"),
                        data.get("size_bytes", 0),
                        data.get("content_hash", ""),
                        data.get("content_version", 1),
                        json.dumps(data.get("provenance") or {}),
                        data.get("status", "pending"),
                        json.dumps(data.get("metadata") or {}),
                        user_id,
                        now,
                        now,
                    ),
                )
                cur.execute("SELECT * FROM notebook_sources WHERE source_id = %s", (source_id,))
                result = _source_row(cur.fetchone())
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_sources(notebook_id: str) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM notebook_sources
                       WHERE notebook_id = %s ORDER BY created_at""",
                    (notebook_id,),
                )
                return [_source_row(item) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def delete_source(notebook_id: str, source_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM notebook_sources WHERE notebook_id = %s AND source_id = %s",
                    (notebook_id, source_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                deleted = _source_row(row)
                cur.execute(
                    "DELETE FROM notebook_sources WHERE notebook_id = %s AND source_id = %s",
                    (notebook_id, source_id),
                )
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def create_conversation(notebook_id: str, data: dict, user_id: str) -> dict:
        conn = get_connection()
        try:
            conversation_id = _id("conv")
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebook_conversations
                       (conversation_id, notebook_id, title, created_by, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (conversation_id, notebook_id, data.get("title", ""), user_id, now, now),
                )
                cur.execute(
                    "SELECT * FROM notebook_conversations WHERE conversation_id = %s",
                    (conversation_id,),
                )
                result = _row(cur.fetchone())
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_conversations(notebook_id: str) -> list[dict]:
        return NotebookDB._list("notebook_conversations", notebook_id, "created_at")

    @staticmethod
    def get_conversation(notebook_id: str, conversation_id: str) -> dict | None:
        return NotebookDB._get_child(
            "notebook_conversations", "conversation_id", conversation_id, notebook_id
        )

    @staticmethod
    def create_event(notebook_id: str, conversation_id: str, data: dict, user_id: str) -> dict:
        conn = get_connection()
        try:
            event_id = _id("evt")
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebook_events
                       (event_id, notebook_id, conversation_id, event_type, message_role,
                        content, metadata, author_user_id, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        event_id,
                        notebook_id,
                        conversation_id,
                        data.get("event_type", "message"),
                        data.get("message_role", "user"),
                        data["content"],
                        json.dumps(data.get("metadata") or {}),
                        user_id,
                        now,
                    ),
                )
                cur.execute("SELECT * FROM notebook_events WHERE event_id = %s", (event_id,))
                result = _row(cur.fetchone(), JSON_FIELDS)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_events(notebook_id: str, conversation_id: str) -> list[dict]:
        return NotebookDB.list_events_page(notebook_id, conversation_id)

    @staticmethod
    def list_events_page(
        notebook_id: str,
        conversation_id: str,
        limit: int = 100,
        cursor: str | None = None,
        include_deleted: bool = False,
        after_event_id: str | None = None,
    ) -> list[dict]:
        conn = get_connection()
        try:
            filters = ["notebook_id = %s", "conversation_id = %s"]
            params = [notebook_id, conversation_id]
            if not include_deleted:
                filters.append("deleted_at IS NULL")
            if cursor:
                filters.append(
                    "(created_at, event_id) > "
                    "(SELECT created_at, event_id FROM notebook_events WHERE event_id = %s)"
                )
                params.append(cursor)
            if after_event_id:
                filters.append(
                    "(created_at, event_id) > "
                    "(SELECT created_at, event_id FROM notebook_events WHERE event_id = %s)"
                )
                params.append(after_event_id)
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT * FROM notebook_events
                        WHERE {' AND '.join(filters)}
                        ORDER BY created_at, event_id
                        LIMIT %s""",
                    params,
                )
                events = [_row(item, JSON_FIELDS) for item in cur.fetchall()]
                for event in events:
                    if event.get("deleted_at"):
                        event["content"] = None
                        event["metadata"] = {}
                return events
        finally:
            release_connection(conn)

    @staticmethod
    def get_event(notebook_id: str, conversation_id: str, event_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM notebook_events
                       WHERE notebook_id = %s AND conversation_id = %s AND event_id = %s""",
                    (notebook_id, conversation_id, event_id),
                )
                return _row(cur.fetchone(), JSON_FIELDS)
        finally:
            release_connection(conn)

    @staticmethod
    def tombstone_event(
        notebook_id: str,
        conversation_id: str,
        event_id: str,
        deleted_by: str,
        reason: str,
    ) -> dict | None:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT content, original_content_hash FROM notebook_events
                       WHERE notebook_id = %s AND conversation_id = %s
                         AND event_id = %s AND deleted_at IS NULL
                       FOR UPDATE""",
                    (notebook_id, conversation_id, event_id),
                )
                current = cur.fetchone()
                if not current:
                    conn.rollback()
                    return None
                content_hash = current["original_content_hash"] or hashlib.sha256(
                    current["content"].encode()
                ).hexdigest()
                cur.execute(
                    """UPDATE notebook_events
                       SET deleted_at = %s, deleted_by = %s, deletion_reason = %s,
                           original_content_hash = %s
                       WHERE notebook_id = %s AND conversation_id = %s
                         AND event_id = %s AND deleted_at IS NULL""",
                    (
                        now,
                        deleted_by,
                        reason,
                        content_hash,
                        notebook_id,
                        conversation_id,
                        event_id,
                    ),
                )
                changed = cur.rowcount > 0
            conn.commit()
            if not changed:
                return None
            event = NotebookDB.get_event(notebook_id, conversation_id, event_id)
            event["content"] = None
            event["metadata"] = {}
            return event
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def create_compaction(
        notebook_id: str,
        conversation_id: str,
        data: dict,
        user_id: str,
    ) -> dict:
        conn = get_connection()
        try:
            compaction_id = _id("cmp")
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT conversation_id FROM notebook_conversations
                       WHERE notebook_id = %s AND conversation_id = %s
                       FOR UPDATE""",
                    (notebook_id, conversation_id),
                )
                if not cur.fetchone():
                    raise NotebookMutationError("Conversation was not found")
                cur.execute(
                    """SELECT event_id, created_at FROM notebook_events
                       WHERE notebook_id = %s AND conversation_id = %s
                         AND event_id = %s AND deleted_at IS NULL
                       FOR SHARE""",
                    (notebook_id, conversation_id, data["cutoff_event_id"]),
                )
                cutoff = cur.fetchone()
                if not cutoff:
                    raise NotebookMutationError("Compaction cutoff event was not found")
                cur.execute(
                    """SELECT COUNT(*) AS event_count FROM notebook_events
                       WHERE notebook_id = %s AND conversation_id = %s
                         AND deleted_at IS NULL
                         AND (created_at, event_id) <= (%s, %s)""",
                    (
                        notebook_id,
                        conversation_id,
                        cutoff["created_at"],
                        cutoff["event_id"],
                    ),
                )
                event_count = cur.fetchone()["event_count"]
                cur.execute(
                    """SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                       FROM conversation_compactions
                       WHERE notebook_id = %s AND conversation_id = %s""",
                    (notebook_id, conversation_id),
                )
                version = cur.fetchone()["next_version"]
                checksum = hashlib.sha256(
                    (
                        data["summary_content"]
                        + "\n"
                        + cutoff["event_id"]
                        + "\n"
                        + str(event_count)
                    ).encode()
                ).hexdigest()
                cur.execute(
                    """INSERT INTO conversation_compactions
                       (compaction_id, notebook_id, conversation_id, version_number,
                        summary_content, cutoff_event_id, cutoff_event_at,
                        originating_user_id, athena_run_metadata, event_count,
                        checksum, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        compaction_id,
                        notebook_id,
                        conversation_id,
                        version,
                        data["summary_content"],
                        cutoff["event_id"],
                        cutoff["created_at"],
                        user_id,
                        json.dumps(data.get("athena_run_metadata") or {}),
                        event_count,
                        checksum,
                        now,
                    ),
                )
                cur.execute(
                    """INSERT INTO engram_events
                       (engram_event_id, notebook_id, event_type, conversation_id,
                        compaction_id, actor_user_id, details, created_at)
                       VALUES (%s, %s, 'compacted', %s, %s, %s, %s, %s)""",
                    (
                        _id("eev"),
                        notebook_id,
                        conversation_id,
                        compaction_id,
                        user_id,
                        json.dumps(
                            {
                                "cutoff_event_id": cutoff["event_id"],
                                "event_count": event_count,
                                "checksum": checksum,
                            }
                        ),
                        now,
                    ),
                )
                cur.execute(
                    "SELECT * FROM conversation_compactions WHERE compaction_id = %s",
                    (compaction_id,),
                )
                result = _row(cur.fetchone(), {"athena_run_metadata": {}})
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_compactions(
        notebook_id: str,
        conversation_id: str,
        limit: int = 50,
    ) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM conversation_compactions
                       WHERE notebook_id = %s AND conversation_id = %s
                       ORDER BY version_number DESC LIMIT %s""",
                    (notebook_id, conversation_id, limit),
                )
                return [
                    _row(item, {"athena_run_metadata": {}})
                    for item in cur.fetchall()
                ]
        finally:
            release_connection(conn)

    @staticmethod
    def latest_compaction(notebook_id: str, conversation_id: str) -> dict | None:
        items = NotebookDB.list_compactions(notebook_id, conversation_id, 1)
        return items[0] if items else None

    @staticmethod
    def set_cover(
        notebook_id: str,
        content: bytes,
        media_type: str,
        style: dict,
        user_id: str,
    ) -> dict:
        conn = get_connection()
        try:
            content_hash = hashlib.sha256(content).hexdigest()
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE notebooks
                       SET cover_content = %s, cover_media_type = %s,
                           cover_size_bytes = %s, cover_hash = %s,
                           cover_style = %s, updated_at = %s
                       WHERE notebook_id = %s""",
                    (
                        content,
                        media_type,
                        len(content),
                        content_hash,
                        json.dumps(style),
                        _now(),
                        notebook_id,
                    ),
                )
            conn.commit()
            return NotebookDB.get_access(notebook_id, user_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def get_cover(notebook_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT cover_content, cover_media_type, cover_size_bytes, cover_hash
                       FROM notebooks WHERE notebook_id = %s""",
                    (notebook_id,),
                )
                return _row(cur.fetchone())
        finally:
            release_connection(conn)

    @staticmethod
    def clear_cover(notebook_id: str, user_id: str) -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE notebooks
                       SET cover_content = NULL, cover_media_type = NULL,
                           cover_size_bytes = NULL, cover_hash = NULL,
                           cover_style = '{}'::jsonb, updated_at = %s
                       WHERE notebook_id = %s""",
                    (_now(), notebook_id),
                )
            conn.commit()
            return NotebookDB.get_access(notebook_id, user_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def provenance_is_valid(notebook_id: str, conversation_id: str | None, event_id: str | None) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if conversation_id:
                    cur.execute(
                        """SELECT 1 FROM notebook_conversations
                           WHERE notebook_id = %s AND conversation_id = %s""",
                        (notebook_id, conversation_id),
                    )
                    if not cur.fetchone():
                        return False
                if event_id:
                    cur.execute(
                        """SELECT conversation_id FROM notebook_events
                           WHERE notebook_id = %s AND event_id = %s""",
                        (notebook_id, event_id),
                    )
                    event = cur.fetchone()
                    if not event:
                        return False
                    if conversation_id and event["conversation_id"] != conversation_id:
                        return False
            return True
        finally:
            release_connection(conn)

    @staticmethod
    def create_memory(notebook_id: str, data: dict, user_id: str) -> dict:
        conn = get_connection()
        try:
            memory_id = _id("mem")
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebook_memories
                       (memory_id, notebook_id, memory_type, title, content, metadata,
                        conversation_id, event_id, author_user_id, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        memory_id,
                        notebook_id,
                        data["memory_type"],
                        data.get("title", ""),
                        data["content"],
                        json.dumps(data.get("metadata") or {}),
                        data.get("conversation_id"),
                        data.get("event_id"),
                        user_id,
                        now,
                        now,
                    ),
                )
                cur.execute("SELECT * FROM notebook_memories WHERE memory_id = %s", (memory_id,))
                result = _row(cur.fetchone(), JSON_FIELDS)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_memories(
        notebook_id: str, limit: int = 100, cursor: str | None = None
    ) -> list[dict]:
        conn = get_connection()
        try:
            params = [notebook_id]
            cursor_filter = ""
            if cursor:
                cursor_filter = """AND (created_at, memory_id) >
                    (SELECT created_at, memory_id FROM notebook_memories
                     WHERE memory_id = %s)"""
                params.append(cursor)
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT * FROM notebook_memories
                        WHERE notebook_id = %s {cursor_filter}
                        ORDER BY created_at, memory_id LIMIT %s""",
                    params,
                )
                return [_row(item, JSON_FIELDS) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def create_artifact(notebook_id: str, data: dict, user_id: str) -> dict:
        conn = get_connection()
        try:
            artifact_id = _id("art")
            now = _now()
            artifact_format = data.get("format", "text")
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebook_artifacts
                       (artifact_id, notebook_id, name, description, format, metadata,
                        created_by, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        artifact_id,
                        notebook_id,
                        data["name"],
                        data.get("description", ""),
                        artifact_format,
                        json.dumps(data.get("metadata") or {}),
                        user_id,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """INSERT INTO notebook_artifact_versions
                       (artifact_id, notebook_id, version_number, content, format,
                        metadata, created_by, created_at)
                       VALUES (%s, %s, 1, %s, %s, %s, %s, %s)""",
                    (
                        artifact_id,
                        notebook_id,
                        data["content"],
                        artifact_format,
                        json.dumps(data.get("version_metadata") or {}),
                        user_id,
                        now,
                    ),
                )
            conn.commit()
            return NotebookDB.get_artifact(notebook_id, artifact_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_artifacts(notebook_id: str) -> list[dict]:
        artifacts = NotebookDB._list(
            "notebook_artifacts", notebook_id, "updated_at DESC", JSON_FIELDS, raw_order=True
        )
        for artifact in artifacts:
            artifact["latest_version"] = NotebookDB.get_latest_artifact_version(
                notebook_id, artifact["artifact_id"]
            )
        return artifacts

    @staticmethod
    def get_artifact(notebook_id: str, artifact_id: str) -> dict | None:
        artifact = NotebookDB._get_child(
            "notebook_artifacts", "artifact_id", artifact_id, notebook_id, JSON_FIELDS
        )
        if artifact:
            artifact["latest_version"] = NotebookDB.get_latest_artifact_version(
                notebook_id, artifact_id
            )
        return artifact

    @staticmethod
    def create_artifact_version(
        notebook_id: str, artifact_id: str, data: dict, user_id: str
    ) -> dict | None:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT format FROM notebook_artifacts
                       WHERE notebook_id = %s AND artifact_id = %s
                       FOR UPDATE""",
                    (notebook_id, artifact_id),
                )
                artifact = cur.fetchone()
                if not artifact:
                    conn.rollback()
                    return None
                cur.execute(
                    """SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                       FROM notebook_artifact_versions
                       WHERE notebook_id = %s AND artifact_id = %s""",
                    (notebook_id, artifact_id),
                )
                version_number = cur.fetchone()["next_version"]
                cur.execute(
                    """INSERT INTO notebook_artifact_versions
                       (artifact_id, notebook_id, version_number, content, format,
                        metadata, created_by, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        artifact_id,
                        notebook_id,
                        version_number,
                        data["content"],
                        data.get("format") or artifact["format"],
                        json.dumps(data.get("metadata") or {}),
                        user_id,
                        now,
                    ),
                )
                cur.execute(
                    "UPDATE notebook_artifacts SET updated_at = %s WHERE artifact_id = %s",
                    (now, artifact_id),
                )
                cur.execute(
                    """SELECT * FROM notebook_artifact_versions
                       WHERE artifact_id = %s AND version_number = %s""",
                    (artifact_id, version_number),
                )
                result = _row(cur.fetchone(), JSON_FIELDS)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_artifact_versions(notebook_id: str, artifact_id: str) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM notebook_artifact_versions
                       WHERE notebook_id = %s AND artifact_id = %s
                       ORDER BY version_number""",
                    (notebook_id, artifact_id),
                )
                return [_row(item, JSON_FIELDS) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def get_artifact_version(
        notebook_id: str, artifact_id: str, version_number: int
    ) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM notebook_artifact_versions
                       WHERE notebook_id = %s AND artifact_id = %s AND version_number = %s""",
                    (notebook_id, artifact_id, version_number),
                )
                return _row(cur.fetchone(), JSON_FIELDS)
        finally:
            release_connection(conn)

    @staticmethod
    def get_latest_artifact_version(notebook_id: str, artifact_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM notebook_artifact_versions
                       WHERE notebook_id = %s AND artifact_id = %s
                       ORDER BY version_number DESC LIMIT 1""",
                    (notebook_id, artifact_id),
                )
                return _row(cur.fetchone(), JSON_FIELDS)
        finally:
            release_connection(conn)

    @staticmethod
    def create_artifact_rendition(
        notebook_id: str,
        artifact_id: str,
        version_number: int,
        data: dict,
        user_id: str,
    ) -> dict:
        conn = get_connection()
        try:
            rendition_id = _id("rnd")
            now = _now()
            content = data.get("content")
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notebook_artifact_renditions
                       (rendition_id, artifact_id, notebook_id, version_number,
                        format, media_type, filename, byte_size, checksum,
                        renderer, renderer_version, renderer_config, status,
                        error, metadata, content, created_by, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        rendition_id,
                        artifact_id,
                        notebook_id,
                        version_number,
                        data["format"],
                        data["media_type"],
                        data["filename"],
                        data["byte_size"],
                        data["checksum"],
                        data["renderer"],
                        data["renderer_version"],
                        json.dumps(data.get("renderer_config") or {}),
                        data["status"],
                        data.get("error", ""),
                        json.dumps(data.get("metadata") or {}),
                        content,
                        user_id,
                        now,
                        now,
                    ),
                )
            conn.commit()
            return NotebookDB.get_artifact_rendition(
                notebook_id, artifact_id, version_number, rendition_id
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_artifact_renditions(
        notebook_id: str,
        artifact_id: str,
        version_number: int,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict]:
        conn = get_connection()
        try:
            params = [notebook_id, artifact_id, version_number]
            cursor_filter = ""
            if cursor:
                cursor_filter = """AND (created_at, rendition_id) <
                    (SELECT created_at, rendition_id
                     FROM notebook_artifact_renditions
                     WHERE rendition_id = %s AND notebook_id = %s)"""
                params.extend([cursor, notebook_id])
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT rendition_id, artifact_id, notebook_id, version_number,
                               format, media_type, filename, byte_size, checksum,
                               renderer, renderer_version, renderer_config, status,
                               error, metadata, created_by, created_at, updated_at
                        FROM notebook_artifact_renditions
                        WHERE notebook_id = %s AND artifact_id = %s
                          AND version_number = %s {cursor_filter}
                        ORDER BY created_at DESC, rendition_id DESC
                        LIMIT %s""",
                    params,
                )
                return [_row(item, RENDITION_JSON_FIELDS) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def get_artifact_rendition(
        notebook_id: str,
        artifact_id: str,
        version_number: int,
        rendition_id: str,
        include_content: bool = False,
    ) -> dict | None:
        conn = get_connection()
        try:
            columns = "*" if include_content else """rendition_id, artifact_id,
                notebook_id, version_number, format, media_type, filename,
                byte_size, checksum, renderer, renderer_version, renderer_config,
                status, error, metadata, created_by, created_at, updated_at"""
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT {columns} FROM notebook_artifact_renditions
                        WHERE notebook_id = %s AND artifact_id = %s
                          AND version_number = %s AND rendition_id = %s""",
                    (notebook_id, artifact_id, version_number, rendition_id),
                )
                item = _row(cur.fetchone(), RENDITION_JSON_FIELDS)
                if item and isinstance(item.get("content"), memoryview):
                    item["content"] = item["content"].tobytes()
                return item
        finally:
            release_connection(conn)

    @staticmethod
    def update_artifact_rendition_status(
        notebook_id: str,
        artifact_id: str,
        version_number: int,
        rendition_id: str,
        status: str,
        error: str,
        metadata: dict,
    ) -> dict | None:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE notebook_artifact_renditions
                       SET status = %s, error = %s,
                           metadata = metadata || %s::jsonb, updated_at = %s
                       WHERE notebook_id = %s AND artifact_id = %s
                         AND version_number = %s AND rendition_id = %s
                         AND status <> 'ready'
                       RETURNING rendition_id""",
                    (
                        status,
                        error,
                        json.dumps(metadata or {}),
                        now,
                        notebook_id,
                        artifact_id,
                        version_number,
                        rendition_id,
                    ),
                )
                changed = cur.fetchone()
            conn.commit()
            if not changed:
                return None
            return NotebookDB.get_artifact_rendition(
                notebook_id, artifact_id, version_number, rendition_id
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def delete_artifact_rendition(
        notebook_id: str,
        artifact_id: str,
        version_number: int,
        rendition_id: str,
    ) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT rendition_id, artifact_id, notebook_id, version_number,
                              format, media_type, filename, byte_size, checksum,
                              renderer, renderer_version, renderer_config,
                              status, error, metadata, created_by, created_at, updated_at
                         FROM notebook_artifact_renditions
                        WHERE notebook_id = %s AND artifact_id = %s
                          AND version_number = %s AND rendition_id = %s""",
                    (notebook_id, artifact_id, version_number, rendition_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                deleted = _row(row, RENDITION_JSON_FIELDS)
                cur.execute(
                    """DELETE FROM notebook_artifact_renditions
                        WHERE notebook_id = %s AND artifact_id = %s
                          AND version_number = %s AND rendition_id = %s""",
                    (notebook_id, artifact_id, version_number, rendition_id),
                )
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def _list(
        table: str,
        notebook_id: str,
        order_column: str,
        json_fields=None,
        raw_order: bool = False,
    ) -> list[dict]:
        allowed = {
            "notebook_sources",
            "notebook_conversations",
            "notebook_memories",
            "notebook_artifacts",
        }
        if table not in allowed:
            raise ValueError("Unsupported notebook table")
        order = order_column if raw_order else f"{order_column} ASC"
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {table} WHERE notebook_id = %s ORDER BY {order}",
                    (notebook_id,),
                )
                return [_row(item, json_fields) for item in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def _get_child(
        table: str,
        id_column: str,
        resource_id: str,
        notebook_id: str,
        json_fields=None,
    ) -> dict | None:
        allowed = {
            ("notebook_conversations", "conversation_id"),
            ("notebook_artifacts", "artifact_id"),
        }
        if (table, id_column) not in allowed:
            raise ValueError("Unsupported notebook child")
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {table} WHERE notebook_id = %s AND {id_column} = %s",
                    (notebook_id, resource_id),
                )
                return _row(cur.fetchone(), json_fields)
        finally:
            release_connection(conn)
