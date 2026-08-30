"""PostgreSQL persistence for notebook Engram v2."""

import hashlib
import json
import uuid

from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


ITEM_JSON = {"metadata": {}}
PROVENANCE_JSON = {}
EVENT_JSON = {"details": {}}
SNAPSHOT_JSON = {"accepted_manifest": []}
RUN_JSON = {"metadata": {}}


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _row(row, json_fields=None):
    return _row_to_dict(row, json_fields=json_fields)


class EngramConflictError(ValueError):
    pass


class EngramDB:
    @staticmethod
    def _ensure_state(cur, notebook_id: str):
        now = _now()
        cur.execute(
            """INSERT INTO notebook_engrams (notebook_id, revision, updated_at)
               VALUES (%s, 0, %s)
               ON CONFLICT (notebook_id) DO NOTHING""",
            (notebook_id, now),
        )

    @staticmethod
    def _state(cur, notebook_id: str, lock: bool = False):
        EngramDB._ensure_state(cur, notebook_id)
        suffix = " FOR UPDATE" if lock else ""
        cur.execute(
            f"SELECT * FROM notebook_engrams WHERE notebook_id = %s{suffix}",
            (notebook_id,),
        )
        return cur.fetchone()

    @staticmethod
    def _append_event(
        cur,
        notebook_id: str,
        event_type: str,
        actor_user_id: str,
        *,
        item_id: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        item_version: int | None = None,
        snapshot_id: str | None = None,
        conversation_id: str | None = None,
        details: dict | None = None,
    ):
        cur.execute(
            """INSERT INTO engram_events
               (engram_event_id, notebook_id, item_id, event_type, from_status,
                to_status, item_version, snapshot_id, conversation_id,
                actor_user_id, details, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                _id("eev"),
                notebook_id,
                item_id,
                event_type,
                from_status,
                to_status,
                item_version,
                snapshot_id,
                conversation_id,
                actor_user_id,
                json.dumps(details or {}),
                _now(),
            ),
        )

    @staticmethod
    def _insert_provenance(
        cur,
        notebook_id: str,
        item_id: str,
        item_version: int,
        entries: list[dict],
        fallback_author: str,
    ):
        now = _now()
        for entry in entries:
            cur.execute(
                """INSERT INTO engram_provenance
                   (provenance_id, notebook_id, item_id, item_version,
                    conversation_id, originating_operator_event_id,
                    athena_event_id, author_user_id, source_id,
                    citation_locator, citation_hash, confidence,
                    extraction_run_id, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s)""",
                (
                    _id("prv"),
                    notebook_id,
                    item_id,
                    item_version,
                    entry.get("conversation_id"),
                    entry.get("originating_operator_event_id")
                    or entry.get("event_id"),
                    entry.get("athena_event_id"),
                    entry.get("author_user_id") or fallback_author,
                    entry.get("source_id"),
                    entry.get("citation_locator", ""),
                    entry.get("citation_hash", ""),
                    entry.get("confidence"),
                    entry.get("extraction_run_id"),
                    now,
                ),
            )

    @staticmethod
    def create_item(notebook_id: str, data: dict, user_id: str) -> dict:
        conn = get_connection()
        try:
            item_id = data.get("item_id") or _id("eng")
            status = data.get("status", "candidate")
            now = _now()
            with conn.cursor() as cur:
                state = EngramDB._state(cur, notebook_id, lock=True)
                expected_revision = data.get("expected_revision")
                if (
                    expected_revision is not None
                    and int(expected_revision) != int(state["revision"])
                ):
                    raise EngramConflictError("Engram revision conflict")
                cur.execute(
                    """INSERT INTO engram_items
                       (item_id, notebook_id, item_type, status, current_version,
                        lock_version, supersedes_item_id, created_by, created_at,
                        updated_at)
                       VALUES (%s, %s, %s, %s, 1, 1, %s, %s, %s, %s)""",
                    (
                        item_id,
                        notebook_id,
                        data["item_type"],
                        status,
                        data.get("supersedes_item_id"),
                        user_id,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """INSERT INTO engram_item_versions
                       (item_id, notebook_id, version_number, title, content,
                        metadata, created_by, created_at)
                       VALUES (%s, %s, 1, %s, %s, %s, %s, %s)""",
                    (
                        item_id,
                        notebook_id,
                        data.get("title", ""),
                        data["content"],
                        json.dumps(data.get("metadata") or {}),
                        user_id,
                        now,
                    ),
                )
                EngramDB._insert_provenance(
                    cur,
                    notebook_id,
                    item_id,
                    1,
                    data.get("provenance") or [],
                    user_id,
                )
                EngramDB._append_event(
                    cur,
                    notebook_id,
                    "extracted",
                    user_id,
                    item_id=item_id,
                    to_status=status,
                    item_version=1,
                    conversation_id=data.get("conversation_id"),
                )
                cur.execute(
                    """UPDATE notebook_engrams
                       SET revision = revision + 1, updated_at = %s
                       WHERE notebook_id = %s""",
                    (now, notebook_id),
                )
            conn.commit()
            return EngramDB.get_item(notebook_id, item_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def get_item(notebook_id: str, item_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT i.*, v.title, v.content, v.metadata,
                              v.created_by AS version_created_by,
                              v.created_at AS version_created_at
                       FROM engram_items i
                       JOIN engram_item_versions v
                         ON v.notebook_id = i.notebook_id
                        AND v.item_id = i.item_id
                        AND v.version_number = i.current_version
                       WHERE i.notebook_id = %s AND i.item_id = %s""",
                    (notebook_id, item_id),
                )
                item = _row(cur.fetchone(), ITEM_JSON)
                if not item:
                    return None
                cur.execute(
                    """SELECT * FROM engram_item_versions
                       WHERE notebook_id = %s AND item_id = %s
                       ORDER BY version_number""",
                    (notebook_id, item_id),
                )
                item["versions"] = [_row(row, ITEM_JSON) for row in cur.fetchall()]
                cur.execute(
                    """SELECT * FROM engram_provenance
                       WHERE notebook_id = %s AND item_id = %s
                       ORDER BY created_at, provenance_id""",
                    (notebook_id, item_id),
                )
                item["provenance"] = [
                    _row(row, PROVENANCE_JSON) for row in cur.fetchall()
                ]
                return item
        finally:
            release_connection(conn)

    @staticmethod
    def list_items(
        notebook_id: str,
        statuses: tuple[str, ...],
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict]:
        conn = get_connection()
        try:
            filters = ["i.notebook_id = %s", "i.status = ANY(%s)"]
            params = [notebook_id, list(statuses)]
            if cursor:
                filters.append(
                    "(i.updated_at, i.item_id) < "
                    "(SELECT updated_at, item_id FROM engram_items WHERE item_id = %s)"
                )
                params.append(cursor)
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT i.*, v.title, v.content, v.metadata
                        FROM engram_items i
                        JOIN engram_item_versions v
                          ON v.notebook_id = i.notebook_id
                         AND v.item_id = i.item_id
                         AND v.version_number = i.current_version
                        WHERE {' AND '.join(filters)}
                        ORDER BY i.updated_at DESC, i.item_id DESC
                        LIMIT %s""",
                    params,
                )
                return [_row(row, ITEM_JSON) for row in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def get_current(notebook_id: str, limit: int = 100) -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                state = EngramDB._state(cur, notebook_id)
            conn.commit()
            return {
                "revision": state["revision"],
                "current_snapshot_id": state["current_snapshot_id"],
                "updated_at": state["updated_at"],
                "items": EngramDB.list_items(
                    notebook_id, ("accepted",), limit=limit
                ),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def batch_decide(
        notebook_id: str,
        item_ids: list[str],
        action: str,
        user_id: str,
        expected_revision: int | None = None,
    ) -> list[dict]:
        target = "accepted" if action == "accept" else "rejected"
        event_type = "accepted" if action == "accept" else "rejected"
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                state = EngramDB._state(cur, notebook_id, lock=True)
                if (
                    expected_revision is not None
                    and int(expected_revision) != int(state["revision"])
                ):
                    raise EngramConflictError("Engram revision conflict")
                cur.execute(
                    """SELECT * FROM engram_items
                       WHERE notebook_id = %s AND item_id = ANY(%s)
                       ORDER BY item_id FOR UPDATE""",
                    (notebook_id, item_ids),
                )
                items = {row["item_id"]: row for row in cur.fetchall()}
                if len(items) != len(item_ids):
                    raise EngramConflictError(
                        "One or more Engram items do not belong to this notebook"
                    )
                for item_id in item_ids:
                    current = items[item_id]
                    if current["status"] not in {"candidate", "accepted", "rejected"}:
                        raise EngramConflictError(
                            f"Item {item_id} cannot transition from {current['status']}"
                        )
                    cur.execute(
                        """UPDATE engram_items
                           SET status = %s, lock_version = lock_version + 1,
                               updated_at = %s
                           WHERE notebook_id = %s AND item_id = %s""",
                        (target, now, notebook_id, item_id),
                    )
                    EngramDB._append_event(
                        cur,
                        notebook_id,
                        event_type,
                        user_id,
                        item_id=item_id,
                        from_status=current["status"],
                        to_status=target,
                        item_version=current["current_version"],
                    )
                cur.execute(
                    """UPDATE notebook_engrams
                       SET revision = revision + %s, updated_at = %s
                       WHERE notebook_id = %s""",
                    (len(item_ids), now, notebook_id),
                )
            conn.commit()
            return [EngramDB.get_item(notebook_id, item_id) for item_id in item_ids]
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def revise_item(
        notebook_id: str,
        item_id: str,
        data: dict,
        user_id: str,
    ) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                EngramDB._state(cur, notebook_id, lock=True)
                cur.execute(
                    """SELECT * FROM engram_items
                       WHERE notebook_id = %s AND item_id = %s FOR UPDATE""",
                    (notebook_id, item_id),
                )
                item = cur.fetchone()
                if not item:
                    raise EngramConflictError("Engram item not found")
                expected = data.get("expected_lock_version")
                if expected is not None and int(expected) != int(item["lock_version"]):
                    raise EngramConflictError("Engram item version conflict")
                version = item["current_version"] + 1
                cur.execute(
                    """SELECT title, content, metadata
                       FROM engram_item_versions
                       WHERE item_id = %s AND version_number = %s""",
                    (item_id, item["current_version"]),
                )
                previous = cur.fetchone()
                cur.execute(
                    """INSERT INTO engram_item_versions
                       (item_id, notebook_id, version_number, title, content,
                        metadata, created_by, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        item_id,
                        notebook_id,
                        version,
                        data.get("title", previous["title"]),
                        data.get("content", previous["content"]),
                        json.dumps(data.get("metadata", previous["metadata"])),
                        user_id,
                        now,
                    ),
                )
                EngramDB._insert_provenance(
                    cur,
                    notebook_id,
                    item_id,
                    version,
                    data.get("provenance") or [],
                    user_id,
                )
                cur.execute(
                    """UPDATE engram_items
                       SET current_version = %s, lock_version = lock_version + 1,
                           updated_at = %s
                       WHERE notebook_id = %s AND item_id = %s""",
                    (version, now, notebook_id, item_id),
                )
                EngramDB._append_event(
                    cur,
                    notebook_id,
                    "revised",
                    user_id,
                    item_id=item_id,
                    from_status=item["status"],
                    to_status=item["status"],
                    item_version=version,
                )
                cur.execute(
                    """UPDATE notebook_engrams SET revision = revision + 1,
                           updated_at = %s WHERE notebook_id = %s""",
                    (now, notebook_id),
                )
            conn.commit()
            return EngramDB.get_item(notebook_id, item_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def transition_item(
        notebook_id: str,
        item_id: str,
        target_status: str,
        event_type: str,
        data: dict,
        user_id: str,
    ) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                EngramDB._state(cur, notebook_id, lock=True)
                cur.execute(
                    """SELECT * FROM engram_items
                       WHERE notebook_id = %s AND item_id = %s FOR UPDATE""",
                    (notebook_id, item_id),
                )
                item = cur.fetchone()
                if not item:
                    raise EngramConflictError("Engram item not found")
                expected = data.get("expected_lock_version")
                if expected is not None and int(expected) != int(item["lock_version"]):
                    raise EngramConflictError("Engram item version conflict")
                cur.execute(
                    """UPDATE engram_items
                       SET status = %s, lock_version = lock_version + 1,
                           resolved_at = CASE WHEN %s = 'resolved' THEN %s ELSE resolved_at END,
                           updated_at = %s
                       WHERE notebook_id = %s AND item_id = %s""",
                    (
                        target_status,
                        target_status,
                        now,
                        now,
                        notebook_id,
                        item_id,
                    ),
                )
                EngramDB._append_event(
                    cur,
                    notebook_id,
                    event_type,
                    user_id,
                    item_id=item_id,
                    from_status=item["status"],
                    to_status=target_status,
                    item_version=item["current_version"],
                    details={"reason": data.get("reason", "")},
                )
                cur.execute(
                    """UPDATE notebook_engrams SET revision = revision + 1,
                           updated_at = %s WHERE notebook_id = %s""",
                    (now, notebook_id),
                )
            conn.commit()
            return EngramDB.get_item(notebook_id, item_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def supersede_item(
        notebook_id: str,
        item_id: str,
        data: dict,
        user_id: str,
    ) -> dict:
        conn = get_connection()
        try:
            now = _now()
            replacement_id = _id("eng")
            with conn.cursor() as cur:
                EngramDB._state(cur, notebook_id, lock=True)
                cur.execute(
                    """SELECT * FROM engram_items
                       WHERE notebook_id = %s AND item_id = %s FOR UPDATE""",
                    (notebook_id, item_id),
                )
                previous = cur.fetchone()
                if not previous:
                    raise EngramConflictError("Engram item not found")
                expected = data.get("expected_lock_version")
                if expected is not None and int(expected) != int(
                    previous["lock_version"]
                ):
                    raise EngramConflictError("Engram item version conflict")
                if previous["status"] in {"superseded", "retracted"}:
                    raise EngramConflictError(
                        f"Item cannot be superseded from {previous['status']}"
                    )
                cur.execute(
                    """INSERT INTO engram_items
                       (item_id, notebook_id, item_type, status, current_version,
                        lock_version, supersedes_item_id, created_by, created_at,
                        updated_at)
                       VALUES (%s, %s, %s, 'accepted', 1, 1, %s, %s, %s, %s)""",
                    (
                        replacement_id,
                        notebook_id,
                        data["item_type"],
                        item_id,
                        user_id,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """INSERT INTO engram_item_versions
                       (item_id, notebook_id, version_number, title, content,
                        metadata, created_by, created_at)
                       VALUES (%s, %s, 1, %s, %s, %s, %s, %s)""",
                    (
                        replacement_id,
                        notebook_id,
                        data.get("title", ""),
                        data["content"],
                        json.dumps(data.get("metadata") or {}),
                        user_id,
                        now,
                    ),
                )
                EngramDB._insert_provenance(
                    cur,
                    notebook_id,
                    replacement_id,
                    1,
                    data.get("provenance") or [],
                    user_id,
                )
                cur.execute(
                    """UPDATE engram_items
                       SET status = 'superseded', lock_version = lock_version + 1,
                           updated_at = %s
                       WHERE notebook_id = %s AND item_id = %s""",
                    (now, notebook_id, item_id),
                )
                EngramDB._append_event(
                    cur,
                    notebook_id,
                    "extracted",
                    user_id,
                    item_id=replacement_id,
                    to_status="accepted",
                    item_version=1,
                    details={"supersedes_item_id": item_id},
                )
                EngramDB._append_event(
                    cur,
                    notebook_id,
                    "superseded",
                    user_id,
                    item_id=item_id,
                    from_status=previous["status"],
                    to_status="superseded",
                    item_version=previous["current_version"],
                    details={
                        "replacement_item_id": replacement_id,
                        "reason": data.get("reason", ""),
                    },
                )
                cur.execute(
                    """UPDATE notebook_engrams SET revision = revision + 2,
                           updated_at = %s WHERE notebook_id = %s""",
                    (now, notebook_id),
                )
            conn.commit()
            return {
                "superseded": EngramDB.get_item(notebook_id, item_id),
                "replacement": EngramDB.get_item(notebook_id, replacement_id),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_timeline(
        notebook_id: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict]:
        conn = get_connection()
        try:
            params = [notebook_id]
            cursor_filter = ""
            if cursor:
                cursor_filter = """AND (created_at, engram_event_id) <
                    (SELECT created_at, engram_event_id FROM engram_events
                     WHERE engram_event_id = %s)"""
                params.append(cursor)
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT * FROM engram_events
                        WHERE notebook_id = %s {cursor_filter}
                        ORDER BY created_at DESC, engram_event_id DESC LIMIT %s""",
                    params,
                )
                return [_row(row, EVENT_JSON) for row in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def create_snapshot(
        notebook_id: str,
        user_id: str,
        expected_revision: int | None = None,
    ) -> dict:
        conn = get_connection()
        try:
            now = _now()
            snapshot_id = _id("snp")
            with conn.cursor() as cur:
                state = EngramDB._state(cur, notebook_id, lock=True)
                if (
                    expected_revision is not None
                    and int(expected_revision) != int(state["revision"])
                ):
                    raise EngramConflictError("Engram revision conflict")
                cur.execute(
                    """SELECT i.item_id, i.item_type, i.current_version,
                              v.title, v.content
                       FROM engram_items i
                       JOIN engram_item_versions v
                         ON v.notebook_id = i.notebook_id
                        AND v.item_id = i.item_id
                        AND v.version_number = i.current_version
                       WHERE i.notebook_id = %s AND i.status = 'accepted'
                       ORDER BY i.item_id""",
                    (notebook_id,),
                )
                manifest = []
                for row in cur.fetchall():
                    manifest.append(
                        {
                            "item_id": row["item_id"],
                            "item_type": row["item_type"],
                            "version": row["current_version"],
                            "title": row["title"],
                            "content_hash": hashlib.sha256(
                                row["content"].encode()
                            ).hexdigest(),
                        }
                    )
                serialized = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                )
                checksum = hashlib.sha256(serialized.encode()).hexdigest()
                cur.execute(
                    """SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                       FROM engram_snapshots WHERE notebook_id = %s""",
                    (notebook_id,),
                )
                version = cur.fetchone()["next_version"]
                cur.execute(
                    """INSERT INTO engram_snapshots
                       (snapshot_id, notebook_id, version_number,
                        parent_snapshot_id, checksum, accepted_manifest,
                        created_by, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        snapshot_id,
                        notebook_id,
                        version,
                        state["current_snapshot_id"],
                        checksum,
                        serialized,
                        user_id,
                        now,
                    ),
                )
                cur.execute(
                    """UPDATE notebook_engrams
                       SET current_snapshot_id = %s, revision = revision + 1,
                           updated_at = %s WHERE notebook_id = %s""",
                    (snapshot_id, now, notebook_id),
                )
                EngramDB._append_event(
                    cur,
                    notebook_id,
                    "reconciled",
                    user_id,
                    snapshot_id=snapshot_id,
                    details={"checksum": checksum, "item_count": len(manifest)},
                )
                cur.execute(
                    "SELECT * FROM engram_snapshots WHERE snapshot_id = %s",
                    (snapshot_id,),
                )
                result = _row(cur.fetchone(), SNAPSHOT_JSON)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_snapshots(notebook_id: str, limit: int = 50) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM engram_snapshots
                       WHERE notebook_id = %s
                       ORDER BY version_number DESC LIMIT %s""",
                    (notebook_id, limit),
                )
                return [_row(row, SNAPSHOT_JSON) for row in cur.fetchall()]
        finally:
            release_connection(conn)

    @staticmethod
    def get_snapshot(notebook_id: str, snapshot_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM engram_snapshots
                       WHERE notebook_id = %s AND snapshot_id = %s""",
                    (notebook_id, snapshot_id),
                )
                return _row(cur.fetchone(), SNAPSHOT_JSON)
        finally:
            release_connection(conn)

    @staticmethod
    def create_extraction_run(notebook_id: str, data: dict, user_id: str) -> dict:
        conn = get_connection()
        try:
            run_id = _id("xrun")
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO engram_extraction_runs
                       (extraction_run_id, notebook_id, conversation_id,
                        extractor_version, model, status, source_fingerprint,
                        metadata, created_by, started_at, completed_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run_id,
                        notebook_id,
                        data.get("conversation_id"),
                        data.get("extractor_version", ""),
                        data.get("model", ""),
                        data.get("status", "running"),
                        data.get("source_fingerprint", ""),
                        json.dumps(data.get("metadata") or {}),
                        user_id,
                        _now(),
                        _now() if data.get("status") in {"completed", "failed"} else None,
                    ),
                )
                cur.execute(
                    """SELECT * FROM engram_extraction_runs
                       WHERE extraction_run_id = %s""",
                    (run_id,),
                )
                result = _row(cur.fetchone(), RUN_JSON)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def list_extraction_runs(notebook_id: str, limit: int = 50) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM engram_extraction_runs
                       WHERE notebook_id = %s ORDER BY started_at DESC LIMIT %s""",
                    (notebook_id, limit),
                )
                return [_row(row, RUN_JSON) for row in cur.fetchall()]
        finally:
            release_connection(conn)
