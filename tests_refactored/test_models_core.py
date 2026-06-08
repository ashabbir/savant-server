from datetime import datetime

from models import (
    Experience,
    JiraNote,
    JiraTicket,
    KGEdge,
    KGNode,
    MRNote,
    MergeRequest,
    Note,
    Notification,
    Task,
    Workspace,
)


def test_model_defaults_and_examples():
    n = Notification(notification_id="n1", event_type="evt", message="hello")
    assert n.read is False
    assert isinstance(n.created_at, datetime)
    assert "example" in Notification.model_config["json_schema_extra"]

    w = Workspace(workspace_id="w1", name="ws")
    assert w.status == "open"
    assert w.task_stats["todo"] == 0

    t = Task(task_id="t1", workspace_id="w1", title="do it")
    assert t.status == "todo"
    assert t.dependencies == []

    note = Note(note_id="n1", session_id="s1", text="x")
    assert note.workspace_id is None

    mr_note = MRNote(text="looks good")
    mr = MergeRequest(
        mr_id="m1",
        workspace_id="w1",
        url="https://gitlab.com/org/repo/-/merge_requests/1",
        project_id="123",
        mr_iid=1,
        title="MR",
        notes=[mr_note],
    )
    assert mr.status == "open"
    assert len(mr.notes) == 1

    jira_note = JiraNote(text="todo")
    jira = JiraTicket(ticket_id="j1", workspace_id="w1", ticket_key="PROJ-1", notes=[jira_note])
    assert jira.status == "todo"
    assert jira.priority == "medium"

    exp = Experience(experience_id="e1", content="c")
    assert exp.source == "note"
    assert exp.files == []

    node = KGNode(node_id="k1", node_type="insight", title="t")
    assert node.status == "staged"
    assert node.metadata == {}

    edge = KGEdge(edge_id="e1", source_id="k1", target_id="k2", edge_type="relates_to")
    assert edge.weight == 1.0
