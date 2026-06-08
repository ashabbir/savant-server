"""
Pydantic models for Savant MongoDB collections
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, ConfigDict, Field


class Notification(BaseModel):
    """Notification collection model for MCP and system events"""
    notification_id: str = Field(..., description="Unique notification identifier")
    event_type: str = Field(..., description="Event type (workspace_created, task_updated, etc)")
    message: str = Field(..., description="Human-readable message")
    detail: Dict[str, Any] = Field(default_factory=dict, description="Additional context data")
    workspace_id: Optional[str] = Field(default=None, description="Related workspace if any")
    session_id: Optional[str] = Field(default=None, description="Related session if any")
    read: bool = Field(default=False, description="Whether notification has been read")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "notification_id": "notif_001",
                "event_type": "workspace_created",
                "message": "Workspace created: Q1 Dev",
                "detail": {"workspace_id": "ws_123", "name": "Q1 Dev"},
                "read": False,
            }
        }
    )


class Workspace(BaseModel):
    """Workspace collection model"""
    workspace_id: str = Field(..., description="Unique workspace identifier")
    name: str = Field(..., description="Workspace name")
    description: Optional[str] = Field(default="", description="Workspace description")
    priority: str = Field(default="medium", description="Priority: critical, high, medium, low")
    status: str = Field(default="open", description="Status: open, closed")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_session_id: Optional[str] = Field(default=None, description="Session that created this workspace")
    task_stats: Dict[str, int] = Field(
        default_factory=lambda: {"todo": 0, "in_progress": 0, "done": 0, "blocked": 0},
        description="Counts of tasks by status"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "workspace_id": "ws_123",
                "name": "Q1 Development",
                "description": "Development work for Q1",
                "priority": "high",
                "status": "open",
            }
        }
    )


class Task(BaseModel):
    """Task collection model"""
    task_id: str = Field(..., description="Unique task identifier")
    workspace_id: str = Field(..., description="Parent workspace ID")
    title: str = Field(..., description="Task title")
    description: Optional[str] = Field(default="", description="Task description")
    status: str = Field(default="todo", description="Status: todo, in_progress, done, blocked")
    priority: str = Field(default="medium", description="Priority: critical, high, medium, low")
    date: Optional[datetime] = Field(default=None, description="Target date for task")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_session_id: Optional[str] = Field(default=None)
    dependencies: List[str] = Field(default_factory=list, description="List of task_ids this depends on")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "task_id": "task_auth_impl",
                "workspace_id": "ws_123",
                "title": "Implement OAuth",
                "status": "in_progress",
                "priority": "high",
            }
        }
    )


class Note(BaseModel):
    """Note collection model (session-agnostic notes)"""
    note_id: str = Field(..., description="Unique note identifier")
    session_id: str = Field(..., description="Session this note belongs to")
    workspace_id: Optional[str] = Field(default=None, description="Associated workspace if any")
    text: str = Field(..., description="Note content")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "note_id": "note_001",
                "session_id": "sess_abc",
                "text": "Remember to review the API contract",
                "workspace_id": "ws_123",
            }
        }
    )


class MRNote(BaseModel):
    """Nested note for merge requests"""
    text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MergeRequest(BaseModel):
    """Merge Request collection model"""
    mr_id: str = Field(..., description="Unique MR identifier")
    workspace_id: str = Field(..., description="Associated workspace")
    url: str = Field(..., description="GitLab MR URL (unique)")
    project_id: str = Field(..., description="GitLab project ID")
    mr_iid: int = Field(..., description="GitLab MR IID")
    title: str = Field(..., description="MR title")
    status: str = Field(default="open", description="Status: draft, open, review, reviewing, approved, merged, closed, on-hold")
    priority: str = Field(default="medium", description="Priority: critical, high, medium, low")
    author: Optional[str] = Field(default=None, description="MR author username")
    jira: Optional[str] = Field(default=None, description="Associated Jira ticket key")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    notes: List[MRNote] = Field(default_factory=list, description="MR notes/comments")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "mr_id": "mr_001",
                "workspace_id": "ws_123",
                "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                "project_id": "123",
                "mr_iid": 42,
                "title": "Add OAuth support",
                "status": "review",
                "author": "jdoe",
            }
        }
    )


class JiraNote(BaseModel):
    """Nested note for Jira tickets"""
    text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class JiraTicket(BaseModel):
    """Jira Ticket collection model"""
    ticket_id: str = Field(..., description="Unique ticket identifier")
    workspace_id: str = Field(..., description="Associated workspace")
    ticket_key: str = Field(..., description="Jira ticket key (e.g., PROJ-1234)")
    title: Optional[str] = Field(default="", description="Ticket title")
    status: str = Field(default="todo", description="Status: todo, in_progress, in_review, done, blocked")
    priority: str = Field(default="medium", description="Priority: critical, high, medium, low")
    assignee: Optional[str] = Field(default=None, description="Assignee username")
    reporter: Optional[str] = Field(default=None, description="Reporter username")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    notes: List[JiraNote] = Field(default_factory=list, description="Ticket notes/comments")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ticket_id": "jira_001",
                "workspace_id": "ws_123",
                "ticket_key": "PROJ-1234",
                "title": "Implement user authentication",
                "status": "in_progress",
                "assignee": "jdoe",
            }
        }
    )


class Experience(BaseModel):
    """Experience/knowledge entry for long-term developer context (legacy — use KGNode)"""
    experience_id: str = Field(..., description="Unique experience identifier")
    content: str = Field(..., description="Experience content text")
    source: str = Field(default="note", description="Source: session, task, or note")
    workspace_id: Optional[str] = Field(default="", description="Associated workspace ID")
    repo: Optional[str] = Field(default="", description="Associated repository name")
    files: List[str] = Field(default_factory=list, description="Related file paths")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "experience_id": "exp_001",
                "content": "Implemented payment service in Scala",
                "source": "session",
                "workspace_id": "ws_123",
                "repo": "payment-service",
                "files": ["PaymentService.scala"],
            }
        }
    )


class KGNode(BaseModel):
    """Knowledge graph node — the atoms of knowledge"""
    node_id: str = Field(..., description="Unique node identifier")
    node_type: str = Field(..., description="Node type: insight, project, session, concept, repo, client, domain, service, library, technology, issue")
    title: str = Field(..., description="Short label shown on graph")
    content: str = Field(default="", description="Long-form content (markdown)")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Type-specific metadata. May include graph_type (str) for namespace classification and workspaces (list[str]) for workspace associations.")
    status: str = Field(default="staged", description="Node status: 'staged' (pending review) or 'committed' (visible in graph)")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class KGEdge(BaseModel):
    """Knowledge graph edge — connections between nodes"""
    edge_id: str = Field(..., description="Unique edge identifier")
    source_id: str = Field(..., description="Source node ID")
    target_id: str = Field(..., description="Target node ID")
    edge_type: str = Field(..., description="Edge type: relates_to, learned_from, applies_to, uses, evolved_from, contributed_to")
    weight: float = Field(default=1.0, description="Connection strength")
    label: str = Field(default="", description="Optional annotation")
    created_at: datetime = Field(default_factory=datetime.utcnow)
