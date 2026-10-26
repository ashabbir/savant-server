"""Persistent embedding identities and retryable contemplation review progress."""
import hashlib
import json

from postgres_client import get_connection, release_connection


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


class ContemplateDB:
    @staticmethod
    def cached_vectors(keys: list[str]) -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT cache_key, vector FROM kg_embedding_cache WHERE cache_key = ANY(%s)', (keys,))
                return {row['cache_key']: row['vector'] for row in cur.fetchall()}
        finally:
            conn.rollback()
            release_connection(conn)

    @staticmethod
    def save_vectors(vectors: dict) -> None:
        if not vectors:
            return
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    'INSERT INTO kg_embedding_cache (cache_key, vector) VALUES %s '
                    'ON CONFLICT (cache_key) DO NOTHING',
                    [(key, json.dumps(vector)) for key, vector in vectors.items()],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def reviews(cur) -> dict:
        cur.execute('SELECT node_id, fingerprint FROM kg_review_progress')
        return {row['node_id']: row['fingerprint'] for row in cur.fetchall()}

    @staticmethod
    def save_review(cur, node_id: str, value: str) -> None:
        cur.execute(
            '''INSERT INTO kg_review_progress (node_id, fingerprint) VALUES (%s,%s)
               ON CONFLICT (node_id) DO UPDATE SET fingerprint=EXCLUDED.fingerprint,
                   reviewed_at=CURRENT_TIMESTAMP''',
            (node_id, value),
        )
