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

    @staticmethod
    def get_leaderboard(days: int = 7) -> dict:
        """Calculate user activity leaderboard with game point scoring.

        Activity point rules:
          - knowledge additions (kg_nodes created): 10 points
          - knowledge lookup & research (savant-knowledge lookups, savant-context research/analyze_code): 5 points
          - search (code_search, structure_search, ast search, etc.): 2 points
        Users without login are ranked at the bottom.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # 1. All registered users
                cur.execute(
                    """SELECT user_id, name, email, role, is_active, last_login_at
                       FROM users
                       ORDER BY created_at ASC"""
                )
                user_rows = cur.fetchall()

                # 2. Tool calls in window
                cur.execute(
                    """SELECT user_id, mcp_server, tool_name, SUM(calls) AS total_calls
                       FROM mcp_tool_usage
                       WHERE day >= (now() AT TIME ZONE 'UTC')::date - %s
                       GROUP BY user_id, mcp_server, tool_name""",
                    (days,),
                )
                tool_rows = cur.fetchall()

                # 3. Knowledge additions in window
                cur.execute(
                    """SELECT created_by, COUNT(*) AS additions
                       FROM kg_nodes
                       WHERE created_at >= (now() - (%s || ' days')::interval)
                       GROUP BY created_by""",
                    (days,),
                )
                kg_rows = cur.fetchall()
        finally:
            release_connection(conn)

        # Map additions
        additions_by_user = {
            r["created_by"]: int(r["additions"]) for r in kg_rows if r.get("created_by")
        }

        # Break down tool usages
        # Activities:
        # - knowledge additions: 10 pts
        # - research & knowledge lookup: 5 pts
        # - search: 2 pts
        research_tools = {"research", "analyze_code"}
        lookup_tools = {"search", "neighbors", "recent", "list_concepts", "list_domains", "project_context", "get_node"}
        search_tools = {"code_search", "structure_search", "memory_bank_search", "search_lossless_tree"}

        stats_by_user: dict[str, dict] = {}
        for row in tool_rows:
            uid = row["user_id"]
            if not uid:
                continue
            st = stats_by_user.setdefault(uid, {
                "search_count": 0,
                "research_count": 0,
                "knowledge_lookup_count": 0,
                "other_tool_calls": 0,
            })
            server = (row["mcp_server"] or "").lower()
            tname = (row["tool_name"] or "").lower()
            calls = int(row["total_calls"])

            if server == "savant-knowledge":
                if tname in lookup_tools or "search" in tname:
                    st["knowledge_lookup_count"] += calls
                else:
                    st["other_tool_calls"] += calls
            elif server == "savant-context":
                if tname in research_tools:
                    st["research_count"] += calls
                elif tname in search_tools or "search" in tname:
                    st["search_count"] += calls
                else:
                    st["other_tool_calls"] += calls
            else:
                if "research" in tname:
                    st["research_count"] += calls
                elif "search" in tname:
                    st["search_count"] += calls
                else:
                    st["other_tool_calls"] += calls

        leaderboard = []
        for u in user_rows:
            uid = u["user_id"]
            user_stats = stats_by_user.get(uid, {
                "search_count": 0,
                "research_count": 0,
                "knowledge_lookup_count": 0,
                "other_tool_calls": 0,
            })
            additions = additions_by_user.get(uid, 0)
            research_count = user_stats["research_count"]
            lookup_count = user_stats["knowledge_lookup_count"]
            search_count = user_stats["search_count"]

            # Points calculation
            knowledge_additions_points = additions * 10
            research_lookup_points = (research_count + lookup_count) * 5
            search_points = search_count * 2
            total_points = knowledge_additions_points + research_lookup_points + search_points

            last_login = u.get("last_login_at")
            has_logged_in = last_login is not None

            leaderboard.append({
                "user_id": uid,
                "name": u.get("name") or uid,
                "email": u.get("email") or "",
                "role": u.get("role") or "user",
                "is_active": bool(u.get("is_active", 1)),
                "last_login_at": last_login.isoformat() if last_login else None,
                "has_logged_in": has_logged_in,
                "points": total_points,
                "knowledge_additions": additions,
                "knowledge_lookups": lookup_count,
                "research": research_count,
                "search": search_count,
                "other_tool_calls": user_stats["other_tool_calls"],
                "total_activity_count": additions + lookup_count + research_count + search_count,
            })

        # Rank users:
        # Those who have logged in come first, sorted by points DESC, then total_activity_count DESC, then name
        # Those who have never logged in come at the bottom (has_logged_in=False)
        leaderboard.sort(key=lambda x: (
            1 if x["has_logged_in"] else 0,
            x["points"],
            x["total_activity_count"],
            1 if x["is_active"] else 0,
        ), reverse=True)

        for idx, entry in enumerate(leaderboard):
            entry["rank"] = idx + 1

        return {
            "days": days,
            "leaderboard": leaderboard,
        }

