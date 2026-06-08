"""NoteDB — PostgreSQL backend."""

from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


class NoteDB:

    @staticmethod
    def _get_by_id_with_conn(note_id: str, conn, user_id: str = "") -> dict | None:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(
                    "SELECT * FROM notes WHERE note_id = %s AND user_id = %s", (note_id, user_id)
                )
            else:
                cur.execute(
                    "SELECT * FROM notes WHERE note_id = %s", (note_id,)
                )
            row = cur.fetchone()
        return _row_to_dict(row)

    @staticmethod
    def create(note: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notes
                       (note_id, session_id, workspace_id, text, created_at, updated_at, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        note["note_id"], note["session_id"],
                        note.get("workspace_id"), note.get("text", ""),
                        note.get("created_at", now), note.get("updated_at", now),
                        note.get("user_id", ""),
                    ),
                )
            conn.commit()
            return NoteDB._get_by_id_with_conn(note["note_id"], conn)
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(note_id: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            return NoteDB._get_by_id_with_conn(note_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_session(session_id: str, limit: int = 100, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM notes WHERE session_id = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (session_id, user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM notes WHERE session_id = %s ORDER BY created_at DESC LIMIT %s",
                        (session_id, limit),
                    )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_workspace(workspace_id: str, limit: int = 100, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM notes WHERE workspace_id = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (workspace_id, user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM notes WHERE workspace_id = %s ORDER BY created_at DESC LIMIT %s",
                        (workspace_id, limit),
                    )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def update(note_id: str, text: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE notes SET text = %s, updated_at = %s WHERE note_id = %s AND user_id = %s",
                        (text, _now(), note_id, user_id),
                    )
                else:
                    cur.execute(
                        "UPDATE notes SET text = %s, updated_at = %s WHERE note_id = %s",
                        (text, _now(), note_id),
                    )
            conn.commit()
            return NoteDB._get_by_id_with_conn(note_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def delete(note_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("DELETE FROM notes WHERE note_id = %s AND user_id = %s", (note_id, user_id))
                else:
                    cur.execute("DELETE FROM notes WHERE note_id = %s", (note_id,))
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def search(text: str, limit: int = 50, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM notes WHERE text ILIKE %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (f"%{text}%", user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM notes WHERE text ILIKE %s ORDER BY created_at DESC LIMIT %s",
                        (f"%{text}%", limit),
                    )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)


NotesDB = NoteDB
