"""Task Management Routes Blueprint for Savant Server."""

import uuid
from datetime import datetime, timedelta
from flask import Blueprint, g, jsonify, request
from db.users import UserDB
from db.tasks import TaskDB
from db.jira_tickets import JiraTicketDB
from routes.preferences import get_user_preference

tasks_bp = Blueprint("tasks", __name__)


def _next_available_workday(start_date_str: str, ended_days=None, work_week=None) -> str:
    """Return next available workday date string skipping non-workdays and ended days."""
    if not isinstance(work_week, (list, tuple, set)):
        work_week = [1, 2, 3, 4, 5]
    if not isinstance(ended_days, (set, list, tuple)):
        ended_days = set()
    else:
        ended_days = set(ended_days)

    valid_workdays = set(work_week)
    use_iso = max(valid_workdays) > 6 if valid_workdays else False

    try:
        dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return start_date_str

    for _ in range(14):
        dt += timedelta(days=1)
        ds = dt.strftime("%Y-%m-%d")
        iso_w = dt.isoweekday()
        js_w = 0 if iso_w == 7 else iso_w
        is_workday = (iso_w in valid_workdays) if use_iso else (js_w in valid_workdays)
        if is_workday and ds not in ended_days:
            return ds
    return start_date_str


@tasks_bp.route("/api/tasks", methods=["GET", "POST"])
def api_tasks():
    user_id = getattr(g, "user_id", "")
    if request.method == "GET":
        workspace_id = request.args.get("workspace_id")
        date = request.args.get("date")
        status = request.args.get("status")
        tasks = TaskDB.list_all(user_id=user_id, workspace_id=workspace_id, date=date, status=status)
        return jsonify(tasks)

    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Task title is required"}), 400

    workspace_id = (data.get("workspace_id") or "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400

    priority = (data.get("priority") or "medium").strip()
    if priority not in ("low", "medium", "high"):
        priority = "medium"

    created = TaskDB.create({
        "task_id": data.get("task_id") or f"tid-{uuid.uuid4().hex[:8]}",
        "title": title,
        "description": data.get("description", ""),
        "workspace_id": workspace_id,
        "status": data.get("status", "todo"),
        "priority": priority,
        "date": data.get("date"),
        "user_id": user_id,
        "depends_on": data.get("depends_on", []),
    })
    return jsonify(created), 200


@tasks_bp.route("/api/tasks/<task_id>", methods=["GET", "PUT", "DELETE"])
def api_task_detail(task_id):
    user_id = getattr(g, "user_id", "")
    task = TaskDB.get_by_id(task_id, user_id=user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    if request.method == "GET":
        return jsonify(task)

    if request.method == "DELETE":
        TaskDB.delete(task_id, user_id=user_id)
        return jsonify({"status": "deleted"}), 200

    data = request.get_json(force=True, silent=True) or {}
    updated = TaskDB.update(task_id, data, user_id=user_id)
    return jsonify(updated)


@tasks_bp.route("/api/tasks/<task_id>/deps", methods=["POST"])
def api_task_add_dep(task_id):
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    depends_on = (data.get("depends_on") or "").strip()
    if not depends_on:
        return jsonify({"error": "depends_on parameter is required"}), 400

    task = TaskDB.get_by_id(task_id, user_id=user_id)
    dep_task = TaskDB.get_by_id(depends_on, user_id=user_id)
    if not task or not dep_task:
        return jsonify({"error": "Task or dependency target not found"}), 404

    updated = TaskDB.add_dependency(task_id, depends_on, user_id=user_id)
    return jsonify(updated)


@tasks_bp.route("/api/tasks/<task_id>/deps/<depends_on>", methods=["DELETE"])
def api_task_remove_dep(task_id, depends_on):
    user_id = getattr(g, "user_id", "")
    task = TaskDB.get_by_id(task_id, user_id=user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if depends_on not in task.get("depends_on", []):
        return jsonify({"error": "Dependency not found"}), 404

    updated = TaskDB.remove_dependency(task_id, depends_on, user_id=user_id)
    return jsonify(updated)


@tasks_bp.route("/api/tasks/ended-days", methods=["GET"])
def api_tasks_ended_days():
    user_id = getattr(g, "user_id", "")
    ended = TaskDB.get_ended_days(user_id=user_id)
    return jsonify(ended)


@tasks_bp.route("/api/tasks/graph", methods=["GET"])
def api_tasks_graph():
    user_id = getattr(g, "user_id", "")
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"error": "workspace_id parameter is required"}), 400

    tasks = TaskDB.list_all(user_id=user_id, workspace_id=workspace_id)
    
    nodes = []
    edges = []
    for t in tasks:
        nodes.append({
            "id": t["task_id"],
            "label": t["title"],
            "status": t.get("status", "todo"),
            "priority": t.get("priority", "medium"),
        })
        for dep in t.get("depends_on", []):
            edges.append({"from": t["task_id"], "to": dep})

    return jsonify({"workspace_id": workspace_id, "nodes": nodes, "edges": edges})


@tasks_bp.route("/api/tasks/jira", methods=["GET"])
def api_tasks_jira():
    user_id = getattr(g, "user_id", "")
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"error": "workspace_id parameter is required"}), 400

    tickets = JiraTicketDB.list_all(user_id=user_id, workspace_id=workspace_id)
    return jsonify(tickets)


@tasks_bp.route("/api/tasks/end-day", methods=["POST"])
def api_tasks_end_day():
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    date = (data.get("date") or "").strip()
    if not date:
        return jsonify({"error": "date parameter is required"}), 400

    pref = get_user_preference("work_week")
    work_week = pref if isinstance(pref, list) else None

    prior_dates = TaskDB.distinct_task_dates_on_or_before(date, user_id=user_id)
    ended_set = set(TaskDB.get_ended_days(user_id=user_id))
    dates_to_end = [d for d in prior_dates if d not in ended_set]
    if date not in dates_to_end and date not in ended_set:
        dates_to_end.append(date)
    if not dates_to_end:
        dates_to_end = [date]

    res = None
    for d in sorted(dates_to_end):
        res = TaskDB.end_day(d, user_id=user_id, work_week=work_week)

    all_ended = TaskDB.get_ended_days(user_id=user_id)
    return jsonify({
        "ok": True,
        "from": date,
        "to": res.get("next_date") if res else date,
        "moved": res.get("moved_count", 0) if res else 0,
        "closed_dates": dates_to_end,
        "ended_days": all_ended,
    })


@tasks_bp.route("/api/tasks/unend-day", methods=["POST"])
def api_tasks_unend_day():
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    date = (data.get("date") or "").strip()
    if not date:
        return jsonify({"error": "date parameter is required"}), 400

    res = TaskDB.unend_day(date, user_id=user_id)
    return jsonify({
        "ok": True,
        "date": date,
        "ended_days": res.get("ended_days", []),
    })
