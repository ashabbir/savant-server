"""Regression tests for TaskDB — data layer."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.tasks import TaskDB


class TestTaskCreate:
    """Task creation must return task_id, seq, and all fields."""

    def test_create_returns_task_id(self, sample_workspace):
        t = TaskDB.create({"task_id": "t-new", "workspace_id": sample_workspace, "title": "New task"})
        assert t["task_id"] == "t-new"

    def test_create_assigns_seq(self, sample_workspace):
        t = TaskDB.create({"task_id": "t-seq", "workspace_id": sample_workspace, "title": "Seq task"})
        assert t["seq"] is not None
        assert isinstance(t["seq"], int)
        assert t["seq"] > 0

    def test_create_seq_increments(self, sample_workspace):
        t1 = TaskDB.create({"task_id": "t-s1", "workspace_id": sample_workspace, "title": "First"})
        t2 = TaskDB.create({"task_id": "t-s2", "workspace_id": sample_workspace, "title": "Second"})
        assert t2["seq"] == t1["seq"] + 1

    def test_create_defaults(self, sample_workspace):
        t = TaskDB.create({"task_id": "t-def", "workspace_id": sample_workspace, "title": "Defaults"})
        assert t["status"] == "todo"
        assert t["priority"] == "medium"
        assert t["depends_on"] == []

    def test_create_with_dependencies(self, sample_workspace):
        t1 = TaskDB.create({"task_id": "t-dep1", "workspace_id": sample_workspace, "title": "Dep target"})
        t2 = TaskDB.create({
            "task_id": "t-dep2", "workspace_id": sample_workspace,
            "title": "Has dep", "depends_on": ["t-dep1"],
        })
        assert t2["depends_on"] == ["t-dep1"]


class TestTaskRead:
    """Tasks must be retrievable by id, seq, and T-XX ref."""

    def test_get_by_id(self, sample_tasks):
        t = TaskDB.get_by_id("tid-1")
        assert t is not None
        assert t["title"] == "Task A"
        assert t["task_id"] == "tid-1"

    def test_get_by_seq(self, sample_tasks):
        t = TaskDB.get_by_id("tid-1")
        seq = t["seq"]
        found = TaskDB.get_by_seq(seq)
        assert found is not None
        assert found["task_id"] == "tid-1"

    def test_resolve_t_ref(self, sample_tasks):
        t = TaskDB.get_by_id("tid-1")
        seq = t["seq"]
        found = TaskDB.resolve_id(f"T-{seq}")
        assert found is not None
        assert found["task_id"] == "tid-1"

    def test_resolve_plain_id(self, sample_tasks):
        found = TaskDB.resolve_id("tid-3")
        assert found is not None
        assert found["title"] == "Task C"

    def test_get_nonexistent(self):
        assert TaskDB.get_by_id("nonexistent") is None
        assert TaskDB.get_by_seq(99999) is None


class TestTaskListFilters:
    """list_all, list_by_date, list_by_workspace must return correct subsets."""

    def test_list_all_returns_everything(self, sample_tasks):
        all_tasks = TaskDB.list_all()
        assert len(all_tasks) == 6

    def test_list_all_by_workspace(self, sample_tasks, sample_workspace):
        ws_tasks = TaskDB.list_all(workspace_id=sample_workspace)
        assert len(ws_tasks) == 6

    def test_list_all_wrong_workspace_empty(self, sample_tasks):
        ws_tasks = TaskDB.list_all(workspace_id="nonexistent-ws")
        assert len(ws_tasks) == 0

    def test_list_by_date(self, sample_tasks):
        day_tasks = TaskDB.list_by_date("2026-03-20")
        assert len(day_tasks) == 3
        titles = {t["title"] for t in day_tasks}
        assert titles == {"Task A", "Task B", "Task C"}

    def test_list_by_date_empty(self, sample_tasks):
        assert TaskDB.list_by_date("2099-01-01") == []

    def test_list_by_workspace_with_status(self, sample_tasks, sample_workspace):
        todo_tasks = TaskDB.list_by_workspace(sample_workspace, status="todo")
        assert len(todo_tasks) == 2
        for t in todo_tasks:
            assert t["status"] == "todo"

    def test_list_by_status(self, sample_tasks):
        done = TaskDB.list_by_status("done")
        assert len(done) == 1
        assert done[0]["title"] == "Task C"


class TestTaskFieldIntegrity:
    """Every task returned must have task_id, seq, depends_on — never None."""

    def test_all_tasks_have_task_id(self, sample_tasks):
        for t in TaskDB.list_all():
            assert t.get("task_id"), f"Task missing task_id: {t}"

    def test_all_tasks_have_seq(self, sample_tasks):
        for t in TaskDB.list_all():
            assert t.get("seq") is not None, f"Task missing seq: {t}"
            assert isinstance(t["seq"], int)

    def test_all_tasks_have_depends_on_list(self, sample_tasks):
        for t in TaskDB.list_all():
            assert "depends_on" in t, f"Task missing depends_on: {t}"
            assert isinstance(t["depends_on"], list)

    def test_all_tasks_have_workspace_id(self, sample_tasks, sample_workspace):
        for t in TaskDB.list_all():
            assert t.get("workspace_id") == sample_workspace

    def test_all_tasks_have_timestamps(self, sample_tasks):
        for t in TaskDB.list_all():
            assert t.get("created_at"), f"Task missing created_at: {t}"
            assert t.get("updated_at"), f"Task missing updated_at: {t}"


class TestTaskUpdate:
    """Status changes, field updates, and workspace moves must persist."""

    def test_update_status(self, sample_tasks):
        updated = TaskDB.update("tid-1", {"status": "in-progress"})
        assert updated["status"] == "in-progress"
        # Re-read to confirm persistence
        t = TaskDB.get_by_id("tid-1")
        assert t["status"] == "in-progress"

    def test_update_preserves_seq(self, sample_tasks):
        t_before = TaskDB.get_by_id("tid-1")
        seq_before = t_before["seq"]
        TaskDB.update("tid-1", {"status": "done"})
        t_after = TaskDB.get_by_id("tid-1")
        assert t_after["seq"] == seq_before

    def test_update_title(self, sample_tasks):
        TaskDB.update("tid-1", {"title": "Renamed"})
        assert TaskDB.get_by_id("tid-1")["title"] == "Renamed"

    def test_update_workspace_id(self, sample_tasks):
        """Moving a task to a different workspace must persist."""
        # Create another workspace
        from db.workspaces import WorkspaceDB
        WorkspaceDB.create({
            "workspace_id": "ws-other",
            "name": "Other WS",
        })
        TaskDB.update("tid-1", {"workspace_id": "ws-other"})
        t = TaskDB.get_by_id("tid-1")
        assert t["workspace_id"] == "ws-other"
        # Original workspace should have one less
        orig = TaskDB.list_all(workspace_id="ws-test-1")
        assert not any(x["task_id"] == "tid-1" for x in orig)

    def test_update_nonexistent_returns_none(self):
        assert TaskDB.update("nonexistent", {"status": "done"}) is None


class TestTaskDependencies:
    """Dependency add/remove and enrichment must work correctly."""

    def test_add_dependency(self, sample_tasks):
        TaskDB.add_dependency("tid-2", "tid-1")
        t = TaskDB.get_by_id("tid-2")
        assert "tid-1" in t["depends_on"]

    def test_remove_dependency(self, sample_tasks):
        TaskDB.add_dependency("tid-2", "tid-1")
        TaskDB.remove_dependency("tid-2", "tid-1")
        t = TaskDB.get_by_id("tid-2")
        assert "tid-1" not in t["depends_on"]

    def test_dependencies_in_list_queries(self, sample_tasks):
        TaskDB.add_dependency("tid-2", "tid-1")
        all_t = TaskDB.list_all()
        t2 = next(t for t in all_t if t["task_id"] == "tid-2")
        assert "tid-1" in t2["depends_on"]


class TestTaskDelete:
    """Deletion must remove task and its dependencies."""

    def test_delete_existing(self, sample_tasks):
        assert TaskDB.delete("tid-1") is True
        assert TaskDB.get_by_id("tid-1") is None
        assert len(TaskDB.list_all()) == 5

    def test_delete_nonexistent(self):
        assert TaskDB.delete("nonexistent") is False

    def test_delete_removes_deps(self, sample_tasks):
        TaskDB.add_dependency("tid-2", "tid-1")
        TaskDB.delete("tid-1")
        t2 = TaskDB.get_by_id("tid-2")
        # dep should be cascade-deleted
        assert "tid-1" not in t2["depends_on"]


class TestMoveIncompleteTasks:
    """Auto-close: incomplete tasks move forward, done tasks stay."""

    def test_move_incomplete(self, sample_tasks):
        moved = TaskDB.move_incomplete_tasks("2026-03-20", "2026-03-23")
        # Task A (todo) and Task B (in-progress) should move; Task C (done) stays
        assert moved == 2
        day20 = TaskDB.list_by_date("2026-03-20")
        assert len(day20) == 1
        assert day20[0]["title"] == "Task C"
        day23 = TaskDB.list_by_date("2026-03-23")
        moved_titles = {t["title"] for t in day23}
        assert "Task A" in moved_titles
        assert "Task B" in moved_titles

    def test_move_preserves_workspace(self, sample_tasks, sample_workspace):
        TaskDB.move_incomplete_tasks("2026-03-20", "2026-03-23")
        for t in TaskDB.list_by_date("2026-03-23"):
            assert t["workspace_id"] == sample_workspace

    def test_move_preserves_seq(self, sample_tasks):
        before = {t["task_id"]: t["seq"] for t in TaskDB.list_all()}
        TaskDB.move_incomplete_tasks("2026-03-20", "2026-03-23")
        after = {t["task_id"]: t["seq"] for t in TaskDB.list_all()}
        for tid in before:
            assert before[tid] == after[tid], f"seq changed for {tid}"


class TestMoveIncompleteTasksOnOrBefore:
    """Cascade-close behaviour: moving every un-done task on or before a
    given date in a single statement, returning the distinct source dates."""

    def test_returns_distinct_source_dates_for_un_done_only(self, sample_tasks):
        moved, sources = TaskDB.move_incomplete_tasks_on_or_before(
            "2026-03-21", "2026-03-25"
        )
        # 2026-03-20 had A(todo), B(in-progress), C(done) → A, B move.
        # 2026-03-21 had D(todo), E(blocked) → both move (blocked != done).
        # Done tasks must stay on 2026-03-20; sources = the two un-done dates.
        assert moved == 4
        assert sources == ["2026-03-20", "2026-03-21"]
        day25 = TaskDB.list_by_date("2026-03-25")
        moved_titles = {t["title"] for t in day25}
        assert {"Task A", "Task B", "Task D", "Task E"} <= moved_titles

    def test_done_tasks_stay_on_their_original_date(self, sample_tasks):
        TaskDB.move_incomplete_tasks_on_or_before("2026-03-21", "2026-03-25")
        day20 = TaskDB.list_by_date("2026-03-20")
        # Only Task C (done) remains on 2026-03-20.
        assert [t["title"] for t in day20] == ["Task C"]

    def test_no_op_when_nothing_incomplete(self, sample_tasks):
        # First sweep moves everything; second should be a no-op.
        TaskDB.move_incomplete_tasks_on_or_before("2026-03-21", "2026-03-25")
        moved, sources = TaskDB.move_incomplete_tasks_on_or_before(
            "2026-03-21", "2026-03-26"
        )
        assert moved == 0
        assert sources == []

    def test_scopes_by_user_id(self, _isolated_db):
        from db.tasks import TaskDB as _T
        from db.workspaces import WorkspaceDB as _W
        _W.create({"workspace_id": "ws-u", "name": "WS", "user_id": "ahmed"})
        _W.create({"workspace_id": "ws-o", "name": "WS Other", "user_id": "other"})
        _T.create({"task_id": "t-mine",  "workspace_id": "ws-u", "title": "Mine",  "date": "2026-04-20", "status": "todo", "user_id": "ahmed"})
        _T.create({"task_id": "t-other", "workspace_id": "ws-o", "title": "Other", "date": "2026-04-20", "status": "todo", "user_id": "other"})
        moved, sources = _T.move_incomplete_tasks_on_or_before(
            "2026-04-21", "2026-04-22", user_id="ahmed"
        )
        # Only the ahmed task should move; the other user's stays put.
        assert moved == 1
        assert sources == ["2026-04-20"]
        other_still_there = [t for t in _T.list_by_date("2026-04-20") if t["user_id"] == "other"]
        assert len(other_still_there) == 1
