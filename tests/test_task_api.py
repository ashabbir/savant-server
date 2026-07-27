"""Regression tests for Task REST API endpoints.

These test the full HTTP round-trip including JSON serialization,
ensuring fields like task_id, seq, depends_on are always present.
"""

import sys, os, json
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _create_workspace(client, name="API Test WS"):
    resp = client.post("/api/workspaces", json={"name": name})
    return resp.get_json()["workspace_id"]


def _create_task(client, ws_id, title="Test task", **kwargs):
    payload = {"title": title, "workspace_id": ws_id, **kwargs}
    return client.post("/api/tasks", json=payload)


@pytest.fixture
def ws(client):
    """Create a workspace and return its id."""
    return _create_workspace(client)


class TestTaskApiCreate:
    """POST /api/tasks must return task with task_id and seq."""

    def test_create_returns_task_id(self, client, ws):
        resp = _create_task(client, ws, title="Hello")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("task_id"), "Response missing task_id"
        assert isinstance(data["task_id"], str)

    def test_create_returns_seq(self, client, ws):
        resp = _create_task(client, ws, title="With seq")
        data = resp.get_json()
        assert data.get("seq") is not None, "Response missing seq"
        assert isinstance(data["seq"], int)
        assert data["seq"] > 0

    def test_create_returns_depends_on(self, client, ws):
        resp = _create_task(client, ws, title="With deps")
        data = resp.get_json()
        assert "depends_on" in data, "Response missing depends_on"
        assert isinstance(data["depends_on"], list)

    def test_create_sets_defaults(self, client, ws):
        resp = _create_task(client, ws, title="Defaults check")
        data = resp.get_json()
        assert data["status"] == "todo"
        assert data["priority"] == "medium"

    def test_create_normalizes_invalid_priority(self, client, ws):
        resp = _create_task(client, ws, title="Priority check", priority="urgent")

        assert resp.status_code == 200
        assert resp.get_json()["priority"] == "medium"

    def test_create_requires_title(self, client, ws):
        resp = client.post("/api/tasks", json={"workspace_id": ws})
        assert resp.status_code == 400


class TestTaskApiList:
    """GET /api/tasks with various filters."""

    def test_list_all(self, client, ws):
        _create_task(client, ws, title="T1")
        _create_task(client, ws, title="T2")
        resp = client.get("/api/tasks")
        data = resp.get_json()
        assert len(data) == 2

    def test_list_by_workspace(self, client):
        ws_a = _create_workspace(client, "WS A")
        ws_b = _create_workspace(client, "WS B")
        _create_task(client, ws_a, title="In A")
        _create_task(client, ws_b, title="In B")
        resp = client.get(f"/api/tasks?workspace_id={ws_a}")
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["title"] == "In A"

    def test_list_by_workspace_returns_all_dates(self, client, ws):
        """REGRESSION: workspace task list must not filter by date."""
        _create_task(client, ws, title="Day 1", date="2099-06-01")
        _create_task(client, ws, title="Day 2", date="2099-06-02")
        _create_task(client, ws, title="Day 3", date="2099-06-03")
        resp = client.get(f"/api/tasks?workspace_id={ws}")
        data = resp.get_json()
        assert len(data) == 3, f"Expected 3 tasks across all dates, got {len(data)}"
        dates = {t["date"] for t in data}
        assert dates == {"2099-06-01", "2099-06-02", "2099-06-03"}

    def test_list_all_tasks_have_task_id(self, client, ws):
        """REGRESSION: every task in list must have task_id (not just 'id')."""
        _create_task(client, ws, title="T1")
        _create_task(client, ws, title="T2")
        resp = client.get("/api/tasks")
        for t in resp.get_json():
            assert t.get("task_id"), f"Task missing task_id: {t}"

    def test_list_all_tasks_have_seq(self, client, ws):
        """REGRESSION: every task in list must have seq number."""
        _create_task(client, ws, title="T1")
        _create_task(client, ws, title="T2")
        resp = client.get("/api/tasks")
        for t in resp.get_json():
            assert t.get("seq") is not None, f"Task missing seq: {t}"

    def test_list_all_tasks_have_depends_on(self, client, ws):
        """REGRESSION: every task in list must have depends_on array."""
        _create_task(client, ws, title="T1")
        resp = client.get("/api/tasks")
        for t in resp.get_json():
            assert "depends_on" in t, f"Task missing depends_on: {t}"
            assert isinstance(t["depends_on"], list)

    def test_list_by_date(self, client, ws):
        _create_task(client, ws, title="Today", date="2026-03-22")
        _create_task(client, ws, title="Tomorrow", date="2026-03-23")
        resp = client.get("/api/tasks?date=2026-03-22&today=2026-03-22")
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Today"

    def test_empty_workspace_returns_empty(self, client, ws):
        resp = client.get(f"/api/tasks?workspace_id={ws}")
        assert resp.get_json() == []


class TestTaskApiUpdate:
    """PUT /api/tasks/<id> must persist changes and return full task."""

    def test_update_status(self, client, ws):
        task = _create_task(client, ws, title="Update me").get_json()
        tid = task["task_id"]
        resp = client.put(f"/api/tasks/{tid}", json={"status": "in-progress"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "in-progress"
        # Verify persistence
        refetch = client.get("/api/tasks").get_json()
        assert refetch[0]["status"] == "in-progress"

    def test_claim_moves_todo_task_to_in_progress_once(self, client, ws):
        task = _create_task(client, ws, title="Claim me").get_json()
        tid = task["task_id"]
        ready = client.post(f"/api/tasks/{tid}/colosseum-ready", json={
            "config": {"repository": "/tmp/repo", "provider": "codex"},
        })
        assert ready.status_code == 200
        claimed = client.post(f"/api/tasks/{tid}/claim")
        assert claimed.status_code == 200
        assert claimed.get_json()["status"] == "in-progress"
        assert client.post(f"/api/tasks/{tid}/claim").status_code == 409

    def test_update_preserves_seq(self, client, ws):
        """REGRESSION: updating a task must not lose its seq number."""
        task = _create_task(client, ws, title="Seq check").get_json()
        original_seq = task["seq"]
        resp = client.put(f"/api/tasks/{task['task_id']}", json={"status": "done"})
        assert resp.get_json()["seq"] == original_seq

    def test_update_preserves_workspace(self, client, ws):
        """REGRESSION: status update must not clear workspace_id."""
        task = _create_task(client, ws, title="WS check").get_json()
        resp = client.put(f"/api/tasks/{task['task_id']}", json={"status": "done"})
        assert resp.get_json()["workspace_id"] == ws

    def test_update_workspace_move(self, client):
        """Moving a task between workspaces must persist."""
        ws_a = _create_workspace(client, "A")
        ws_b = _create_workspace(client, "B")
        task = _create_task(client, ws_a, title="Movable").get_json()
        resp = client.put(f"/api/tasks/{task['task_id']}", json={"workspace_id": ws_b})
        assert resp.get_json()["workspace_id"] == ws_b
        # Must now appear in ws-b, not ws-a
        a_tasks = client.get(f"/api/tasks?workspace_id={ws_a}").get_json()
        b_tasks = client.get(f"/api/tasks?workspace_id={ws_b}").get_json()
        assert len(a_tasks) == 0
        assert len(b_tasks) == 1

    def test_update_nonexistent_404(self, client):
        resp = client.put("/api/tasks/fake-id", json={"status": "done"})
        assert resp.status_code == 404


class TestTaskApiDependencies:
    """POST/DELETE /api/tasks/<id>/deps must persist dependency links."""

    def test_add_dependency(self, client, ws):
        parent = _create_task(client, ws, title="Test2").get_json()
        dep = _create_task(client, ws, title="Test1").get_json()

        resp = client.post(f"/api/tasks/{parent['task_id']}/deps", json={"depends_on": dep["task_id"]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert dep["task_id"] in data["depends_on"]

    def test_add_dependency_missing_task_404(self, client, ws):
        task = _create_task(client, ws, title="Test2").get_json()
        resp = client.post(f"/api/tasks/{task['task_id']}/deps", json={"depends_on": "missing"})
        assert resp.status_code == 404

    def test_remove_dependency(self, client, ws):
        parent = _create_task(client, ws, title="Test2").get_json()
        dep = _create_task(client, ws, title="Test1").get_json()
        client.post(f"/api/tasks/{parent['task_id']}/deps", json={"depends_on": dep["task_id"]})

        resp = client.delete(f"/api/tasks/{parent['task_id']}/deps/{dep['task_id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert dep["task_id"] not in data["depends_on"]

    def test_remove_dependency_missing_404(self, client, ws):
        task = _create_task(client, ws, title="Test2").get_json()
        resp = client.delete(f"/api/tasks/{task['task_id']}/deps/missing")
        assert resp.status_code == 404


class TestTaskApiDelete:
    """DELETE /api/tasks/<id> must remove the task."""

    def test_delete_task(self, client, ws):
        task = _create_task(client, ws, title="Delete me").get_json()
        resp = client.delete(f"/api/tasks/{task['task_id']}")
        assert resp.status_code == 200
        # Verify gone
        remaining = client.get("/api/tasks").get_json()
        assert len(remaining) == 0

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/tasks/fake-id")
        assert resp.status_code in (404, 200)


class TestTaskApiEndDay:
    """End-day endpoints must exist and return JSON."""

    def test_list_ended_days_defaults_empty(self, client):
        resp = client.get("/api/tasks/ended-days")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_end_day_marks_day_as_ended(self, client, ws):
        # 2026-04-22 is a Wednesday — next available workday is Thursday.
        _create_task(client, ws, title="Carry over", date="2026-04-22")
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-22"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["from"] == "2026-04-22"
        assert data["to"] == "2026-04-23"  # Thu, next available workday
        ended = client.get("/api/tasks/ended-days").get_json()
        assert "2026-04-22" in ended

    def test_end_day_skips_weekend_to_next_workday(self, client, ws):
        """2026-04-24 is a Friday; rolling forward must skip Sat+Sun and
        land on Monday (the next available workday under the default
        Mon–Fri preference)."""
        _create_task(client, ws, title="Friday todo", date="2026-04-24")
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-24"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["to"] == "2026-04-27"  # skips Sat 25 + Sun 26

    def test_end_day_skips_already_ended_workday(self, client, ws):
        """If the literal next workday is already ended, jump further out."""
        # Close Monday 2026-04-27 first.
        client.post("/api/tasks/end-day", json={"date": "2026-04-27"})
        # Now close the prior Friday — it must skip Sat/Sun AND the already-
        # ended Monday, landing on Tuesday 2026-04-28.
        _create_task(client, ws, title="Late Friday", date="2026-04-24")
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-24"})
        assert resp.status_code == 200
        assert resp.get_json()["to"] == "2026-04-28"

    def test_unend_day_removes_day(self, client, ws):
        client.post("/api/tasks/end-day", json={"date": "2026-04-24"})
        resp = client.post("/api/tasks/unend-day", json={"date": "2026-04-24"})
        assert resp.status_code == 200
        ended = client.get("/api/tasks/ended-days").get_json()
        assert "2026-04-24" not in ended

    def test_end_day_cascades_to_prior_open_days(self, client, ws):
        """Ending Wednesday must also close Mon/Tue that were left open
        and roll all their incomplete tasks into the next workday."""
        # Three earlier days with incomplete tasks; one with a done task that
        # should be left alone. 2026-04-20 = Monday, …, 2026-04-22 = Wed.
        t1 = _create_task(client, ws, title="Mon todo",  date="2026-04-20").get_json()
        t2 = _create_task(client, ws, title="Tue todo",  date="2026-04-21").get_json()
        t3 = _create_task(client, ws, title="Wed todo",  date="2026-04-22").get_json()
        t4 = _create_task(client, ws, title="Tue done",  date="2026-04-21").get_json()
        client.put(f"/api/tasks/{t4['task_id']}", json={"status": "done"})

        # User clicks "End Day" on Wednesday.
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-22"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["to"] == "2026-04-23"  # Thursday
        # 3 incomplete tasks moved; 1 done task untouched.
        assert data["moved"] == 3

        # All three touched dates plus Wed should now be ended.
        ended = set(client.get("/api/tasks/ended-days").get_json())
        for d in ("2026-04-20", "2026-04-21", "2026-04-22"):
            assert d in ended, f"expected {d} to be ended after cascade"

        rows_to = client.get(f"/api/tasks?date=2026-04-23").get_json()
        moved_ids = {r["task_id"] for r in rows_to}
        assert {t1["task_id"], t2["task_id"], t3["task_id"]} <= moved_ids

        rows_21 = client.get(f"/api/tasks?date=2026-04-21").get_json()
        assert any(r["task_id"] == t4["task_id"] for r in rows_21), "done task must stay on its original day"

    def test_end_day_with_no_prior_open_days_just_closes_today(self, client, ws):
        """Sanity: when nothing's stale, end-day still works like before."""
        _create_task(client, ws, title="Today", date="2026-04-22")
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-22"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["closed_dates"] == ["2026-04-22"]
        assert data["moved"] == 1

    def test_end_day_cascade_closes_days_with_only_done_tasks(self, client, ws):
        """A day where every task is already done should still be marked
        ended when a later day cascades — the user wants a clean
        calendar, not stale 'open' badges on completed days."""
        td = _create_task(client, ws, title="Mon done", date="2026-04-20").get_json()
        client.put(f"/api/tasks/{td['task_id']}", json={"status": "done"})
        # No incomplete tasks on Monday, but it WAS touched.
        _create_task(client, ws, title="Wed todo", date="2026-04-22")
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-22"})
        assert resp.status_code == 200
        ended = set(client.get("/api/tasks/ended-days").get_json())
        assert "2026-04-20" in ended, "fully-done prior day must be marked ended"

    def test_end_day_after_reopen_still_cascades(self, client, ws):
        """Regression for the user-reported bug: reopen a previously closed
        day, then click End Day later — the reopened day must close again."""
        client.post("/api/tasks/end-day", json={"date": "2026-04-20"})
        client.post("/api/tasks/unend-day", json={"date": "2026-04-20"})
        _create_task(client, ws, title="Late add", date="2026-04-20")
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-22"})
        assert resp.status_code == 200
        ended = set(client.get("/api/tasks/ended-days").get_json())
        assert "2026-04-20" in ended
        assert "2026-04-22" in ended

    def test_end_day_respects_user_work_week_preference(self, client, ws):
        """If the user works Tue–Sat, ending Saturday should roll forward
        to next Tuesday (skipping Sun + Mon)."""
        # 2026-04-21 = Tue, 22 = Wed, 23 = Thu, 24 = Fri, 25 = Sat,
        # 26 = Sun, 27 = Mon, 28 = Tue.
        # JS weekday convention: Sun=0..Sat=6. Tue–Sat = [2,3,4,5,6].
        client.post("/api/preferences", json={"work_week": [2, 3, 4, 5, 6]})
        _create_task(client, ws, title="Sat todo", date="2026-04-25")
        resp = client.post("/api/tasks/end-day", json={"date": "2026-04-25"})
        assert resp.status_code == 200
        assert resp.get_json()["to"] == "2026-04-28"  # next Tue


class TestTaskApiJira:
    """Workspace Jira tab needs a dedicated JSON endpoint."""

    def test_jira_workspace_endpoint_returns_json(self, client, ws):
        from db.jira_tickets import JiraTicketDB

        JiraTicketDB.create({
            "ticket_id": "jira-task-api-1",
            "workspace_id": ws,
            "ticket_key": "TASK-1",
            "title": "Task Jira",
            "status": "todo",
            "priority": "medium",
            "reporter": "Tester",
            "assignee": "Owner",
        })

        resp = client.get(f"/api/tasks/jira?workspace_id={ws}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["ticket_key"] == "TASK-1"

    def test_jira_workspace_endpoint_requires_workspace_id(self, client):
        resp = client.get("/api/tasks/jira")
        assert resp.status_code == 400


class TestMergeRequestApi:
    """Workspace merge-request tab needs a dedicated JSON endpoint."""

    def test_merge_request_workspace_endpoint_returns_json(self, client, ws):
        from db.merge_requests import MergeRequestDB

        MergeRequestDB.create({
            "mr_id": "mr-task-api-1",
            "workspace_id": ws,
            "url": "https://gitlab.com/team/repo/-/merge_requests/101",
            "status": "open",
            "author": "Tester",
            "jira": "TASK-1",
        })

        resp = client.get(f"/api/merge-requests?workspace_id={ws}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["url"].endswith("/101")

    def test_merge_request_workspace_endpoint_requires_workspace_id(self, client):
        resp = client.get("/api/merge-requests")
        assert resp.status_code == 400


class TestSearchTaskRegression:
    """Workspace search must return task id and seq."""

    def test_search_returns_task_id(self, client, ws):
        _create_task(client, ws, title="Searchable unicorn task")
        resp = client.get("/api/workspaces/search?q=unicorn")
        data = resp.get_json()
        tasks = data.get("tasks", [])
        assert len(tasks) == 1
        assert tasks[0].get("id"), "Search result missing id"

    def test_search_returns_seq(self, client, ws):
        _create_task(client, ws, title="Searchable dragon task")
        resp = client.get("/api/workspaces/search?q=dragon")
        data = resp.get_json()
        tasks = data.get("tasks", [])
        assert len(tasks) == 1
        assert tasks[0].get("seq") is not None, "Search result missing seq"


class TestTaskApiGraph:
    """GET /api/tasks/graph must return nodes and dependency edges."""

    def test_graph_returns_workspace_tasks(self, client, ws):
        parent = _create_task(client, ws, title="Parent", priority="high").get_json()
        child = _create_task(client, ws, title="Child", priority="low").get_json()
        client.post(f"/api/tasks/{parent['task_id']}/deps", json={"depends_on": child["task_id"]})

        resp = client.get(f"/api/tasks/graph?workspace_id={ws}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["workspace_id"] == ws
        assert {n["id"] for n in data["nodes"]} == {parent["task_id"], child["task_id"]}
        assert {"from": parent["task_id"], "to": child["task_id"]} in data["edges"]

    def test_graph_requires_workspace_id(self, client):
        resp = client.get("/api/tasks/graph")
        assert resp.status_code == 400
