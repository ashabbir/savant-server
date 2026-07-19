"""Modular Routes Package for Savant Server."""

from routes.users import users_bp
from routes.workspaces import workspaces_bp
from routes.tasks import tasks_bp
from routes.jira_mr import jira_mr_bp
from routes.preferences import preferences_bp
from routes.jobs_system import jobs_system_bp
from routes.sessions import sessions_bp

__all__ = [
    "users_bp",
    "workspaces_bp",
    "tasks_bp",
    "jira_mr_bp",
    "preferences_bp",
    "jobs_system_bp",
    "sessions_bp",
]
