"""Persistence for repository-scoped code-intelligence operational metadata."""

from db.base import _now
from postgres_client import get_connection, release_connection


class CodeIntelligenceConfigDB:
    @staticmethod
    def get(repo_id: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.*, r.name AS repo_name, r.path AS repo_path
                       FROM ctx_repos r
                       LEFT JOIN code_intelligence_config c ON c.repo_id = r.id
                       WHERE r.name = %s OR r.id::text = %s""",
                    (str(repo_id), str(repo_id)),
                )
                row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            result["provider"] = "codegraph"
            result["freshness"] = result.get("freshness") or "unavailable"
            result["rollout_state"] = "codegraph_primary"
            result["watch_enabled"] = bool(result.get("watch_enabled"))
            return result
        finally:
            release_connection(conn)

    @staticmethod
    def provider_for_repo(repo_id: str) -> str | None:
        config = CodeIntelligenceConfigDB.get(repo_id)
        return config.get("provider") if config else None

    @staticmethod
    def upsert(repo_id: str, **fields) -> dict:
        allowed = {
            "provider", "index_root", "engine_version", "graph_version",
            "last_indexed_at", "last_synced_at", "freshness", "last_error_code",
            "last_error_at", "watch_enabled", "rollout_state",
        }
        changes = {key: value for key, value in fields.items() if key in allowed}
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM ctx_repos WHERE name = %s OR id::text = %s",
                    (str(repo_id), str(repo_id)),
                )
                repo = cur.fetchone()
                if not repo:
                    raise LookupError(f"repository not found: {repo_id}")
                columns = ["repo_id", *changes, "updated_at"]
                values = [repo["id"], *changes.values(), _now()]
                updates = ", ".join(f"{column} = EXCLUDED.{column}" for column in [*changes, "updated_at"])
                placeholders = ", ".join(["%s"] * len(values))
                cur.execute(
                    f"""INSERT INTO code_intelligence_config ({', '.join(columns)})
                        VALUES ({placeholders})
                        ON CONFLICT (repo_id) DO UPDATE SET {updates}""",
                    values,
                )
            conn.commit()
        finally:
            release_connection(conn)
        return CodeIntelligenceConfigDB.get(repo_id)
