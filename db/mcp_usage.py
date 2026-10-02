"""McpUsageDB — daily per-user MCP tool call counters."""

from postgres_client import get_connection, release_connection

RECENT_QUERY_LIMIT = 50


class McpUsageDB:

    @staticmethod
    def record_call(
        user_id: str,
        mcp_server: str,
        tool_name: str,
        projects: list[tuple[str, str]] | None = None,
        query: str = "",
        repo: str = "",
    ) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO mcp_tool_usage
                           (user_id, mcp_server, tool_name, day, calls, last_called_at)
                       VALUES (%s, %s, %s, (now() AT TIME ZONE 'UTC')::date, 1, now())
                       ON CONFLICT (user_id, mcp_server, tool_name, day) DO UPDATE SET
                         calls = mcp_tool_usage.calls + 1,
                         last_called_at = EXCLUDED.last_called_at""",
                    (user_id, mcp_server, tool_name),
                )
                for project_type, project in projects or []:
                    cur.execute(
                        """INSERT INTO mcp_project_usage
                               (user_id, project_type, project, day, calls, last_used_at)
                           VALUES (%s, %s, %s, (now() AT TIME ZONE 'UTC')::date, 1, now())
                           ON CONFLICT (user_id, project_type, project, day) DO UPDATE SET
                             calls = mcp_project_usage.calls + 1,
                             last_used_at = EXCLUDED.last_used_at""",
                        (user_id, project_type, project),
                    )
                if query:
                    cur.execute(
                        """INSERT INTO mcp_query_log (user_id, mcp_server, tool_name, query, repo)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (user_id, mcp_server, tool_name, query, repo),
                    )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def get_user_usage(user_id: str, days: int = 30) -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT mcp_server, tool_name, day, calls, last_called_at
                       FROM mcp_tool_usage
                       WHERE user_id = %s
                         AND day > (now() AT TIME ZONE 'UTC')::date - %s
                       ORDER BY day DESC, calls DESC, mcp_server, tool_name""",
                    (user_id, days),
                )
                rows = cur.fetchall()
                cur.execute(
                    """SELECT day, logins FROM user_login_days
                       WHERE user_id = %s AND day > (now() AT TIME ZONE 'UTC')::date - %s
                       ORDER BY day DESC""",
                    (user_id, days),
                )
                login_rows = cur.fetchall()
                cur.execute(
                    """SELECT p.project_type, p.project, COALESCE(w.name, '') AS project_name,
                              SUM(p.calls) AS calls, COUNT(*) AS active_days,
                              MAX(p.last_used_at) AS last_used_at
                       FROM mcp_project_usage p
                       LEFT JOIN workspaces w
                         ON p.project_type = 'workspace' AND w.workspace_id = p.project
                       WHERE p.user_id = %s AND p.day > (now() AT TIME ZONE 'UTC')::date - %s
                       GROUP BY p.project_type, p.project, w.name
                       ORDER BY calls DESC, p.project""",
                    (user_id, days),
                )
                project_rows = cur.fetchall()
                cur.execute(
                    """SELECT mcp_server, tool_name, query, repo, created_at
                       FROM mcp_query_log
                       WHERE user_id = %s AND created_at > now() - make_interval(days => %s)
                       ORDER BY created_at DESC
                       LIMIT %s""",
                    (user_id, days, RECENT_QUERY_LIMIT),
                )
                query_rows = cur.fetchall()
        finally:
            release_connection(conn)

        daily = []
        totals: dict[tuple[str, str], dict] = {}
        for row in rows:
            r = dict(row)
            day = r["day"].isoformat()
            last = r["last_called_at"].isoformat() if r["last_called_at"] else None
            daily.append({
                "day": day,
                "mcp_server": r["mcp_server"],
                "tool_name": r["tool_name"],
                "calls": r["calls"],
            })
            key = (r["mcp_server"], r["tool_name"])
            t = totals.setdefault(key, {
                "mcp_server": r["mcp_server"],
                "tool_name": r["tool_name"],
                "calls": 0,
                "active_days": 0,
                "last_called_at": last,
            })
            t["calls"] += r["calls"]
            t["active_days"] += 1
            if last and (not t["last_called_at"] or last > t["last_called_at"]):
                t["last_called_at"] = last

        tools = sorted(totals.values(), key=lambda t: (-t["calls"], t["mcp_server"], t["tool_name"]))
        for t in tools:
            t["avg_calls_per_active_day"] = round(t["calls"] / t["active_days"], 1)
        return {
            "days": days,
            "total_calls": sum(t["calls"] for t in tools),
            "tools": tools,
            "daily": daily,
            "logins_per_day": [
                {"day": r["day"].isoformat(), "logins": r["logins"]} for r in login_rows
            ],
            "projects": [
                {
                    "project_type": r["project_type"],
                    "project": r["project"],
                    "project_name": r["project_name"] or r["project"],
                    "calls": int(r["calls"]),
                    "active_days": int(r["active_days"]),
                    "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
                }
                for r in project_rows
            ],
            "recent_queries": [
                {
                    "mcp_server": r["mcp_server"],
                    "tool_name": r["tool_name"],
                    "query": r["query"],
                    "repo": r["repo"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in query_rows
            ],
        }
