"""Task Management Routes Blueprint for Savant Server."""

import os
import uuid
from datetime import datetime, timedelta
from flask import Blueprint, g, jsonify, request
from db.users import UserDB
from db.tasks import TaskDB
from db.jira_tickets import JiraTicketDB
from routes.preferences import get_user_preference

tasks_bp = Blueprint("tasks", __name__)

COLOSSEUM_PROVIDERS = {"hermes", "codex", "claude", "copilot", "agy"}


def _validate_colosseum_config(data):
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return None, "config must be an object"
    repository = str(config.get("repository") or "").strip()
    work_type = str(config.get("work_type") or ("development" if repository else "research")).strip().lower()
    if work_type not in {"development", "research"}:
        return None, "config.work_type must be development or research"
    provider = str(config.get("provider") or "").strip().lower()
    if provider and provider not in COLOSSEUM_PROVIDERS:
        return None, "config.provider must be an installed Colosseum provider"
    timeout = config.get("timeout_seconds", 3600)
    if not isinstance(timeout, int) or not 0 < timeout <= 86400:
        return None, "config.timeout_seconds must be between 1 and 86400"
    return {
        **config,
        "repository": repository,
        "work_type": work_type,
        "autopilot": bool(config.get("autopilot", False)),
        "provider": provider,
        "persona": config.get("persona", ""),
        "tags": config.get("tags", []),
        "model": config.get("model", ""),
    }, None


def _comment(task: dict, text: str, author: str, role: str) -> tuple[dict, list[dict]]:
    comment = {
        "id": f"c-{uuid.uuid4().hex[:8]}",
        "author": author,
        "text": text,
        "role": role,
        "createdAt": datetime.utcnow().isoformat() + "Z",
    }
    comments = list(task.get("comments") or [])
    comments.append(comment)
    return comment, comments


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


@tasks_bp.route("/api/tasks/<task_id>/comments", methods=["GET", "POST"])
def api_task_comments(task_id):
    user_id = getattr(g, "user_id", "")
    task = TaskDB.get_by_id(task_id, user_id=user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    if request.method == "GET":
        return jsonify(task.get("comments", []))

    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Comment text is required"}), 400

    author = data.get("author") or "agent"
    role = data.get("role") or "agent"
    comment_obj, existing_comments = _comment(task, text, author, role)
    TaskDB.update(task_id, {"comments": existing_comments}, user_id=user_id)
    return jsonify(comment_obj), 200


@tasks_bp.route("/api/tasks/<task_id>/claim", methods=["POST"])
def api_task_claim(task_id):
    """Claim a todo task for a single execution worker.

    Returning 409 rather than silently updating an already-active task keeps
    multiple runners from executing the same development task.
    """
    user_id = getattr(g, "user_id", "")
    claimed = TaskDB.claim_todo(task_id, user_id=user_id)
    if not claimed:
        return jsonify({"error": "Task is not available to claim"}), 409
    return jsonify(claimed), 200


@tasks_bp.route("/api/tasks/<task_id>/colosseum-ready", methods=["POST"])
def api_task_colosseum_ready(task_id):
    user_id = getattr(g, "user_id", "")
    task = TaskDB.get_by_id(task_id, user_id=user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if task.get("status") in ("done", "blocked"):
        return jsonify({"error": "Only an open task can be made ready for Colosseum"}), 409
    config, error = _validate_colosseum_config(request.get_json(force=True, silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    return jsonify(TaskDB.set_colosseum_ready(task_id, config, user_id=user_id)), 200


@tasks_bp.route("/api/tasks/colosseum/next", methods=["GET"])
def api_next_colosseum_task():
    user_id = getattr(g, "user_id", "")
    workspace_id = request.args.get("workspace_id") or None
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    phases = ("approved", "review", "ready", "grooming")
    all_colosseum_tasks = [
        task
        for phase in phases
        for task in TaskDB.list_all(workspace_id=workspace_id, user_id=user_id, status=phase)
        if task.get("colosseum_ready")
    ]
    if not all_colosseum_tasks and user_id:
        all_colosseum_tasks = [
            task
            for phase in phases
            for task in TaskDB.list_all(workspace_id=workspace_id, user_id="", status=phase)
            if task.get("colosseum_ready")
        ]
    if not all_colosseum_tasks:
        return jsonify({"message": "No queued Colosseum task", "workspace_id": workspace_id}), 200
    phase_rank = {phase: index for index, phase in enumerate(phases)}
    all_colosseum_tasks.sort(key=lambda task: (phase_rank.get(task.get("status"), 9), rank.get(task.get("priority"), 2)))
    selected = all_colosseum_tasks[0]
    config = dict(selected.get("colosseum_config") or {})
    if not config.get("repository"):
        config["repository"] = selected.get("repository") or os.getcwd()
    settings_key = "colosseum:review-settings" if selected.get("status") == "review" else "colosseum:ready-settings"
    phase_settings = get_user_preference(settings_key, {})
    config["provider"] = config.get("provider") or phase_settings.get("provider") or "codex"
    if phase_settings.get("persona"):
        config["persona"] = phase_settings.get("persona")
    if phase_settings.get("tags"):
        config["tags"] = [t.strip() for t in str(phase_settings.get("tags")).split(",") if t.strip()]
    if phase_settings.get("model"):
        config["model"] = phase_settings.get("model")
    selected["colosseum_config"] = config
    return jsonify(selected), 200


@tasks_bp.route("/api/tasks/<task_id>/colosseum-ready-state", methods=["POST"])
def api_task_colosseum_ready_state(task_id):
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data.get("ready"), bool):
        return jsonify({"error": "ready must be a boolean"}), 400
    updated = TaskDB.set_colosseum_ready_state(task_id, data["ready"], user_id=user_id)
    if not updated:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(updated), 200


@tasks_bp.route("/api/tasks/<task_id>/colosseum-metadata", methods=["PUT"])
def api_task_colosseum_metadata(task_id):
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "metadata must be an object"}), 400
    updated = TaskDB.patch_colosseum_config(task_id, data, user_id=user_id)
    if not updated:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(updated), 200


@tasks_bp.route("/api/tasks/<task_id>/colosseum-runs", methods=["GET", "POST"])
def api_task_colosseum_runs(task_id):
    user_id = getattr(g, "user_id", "")
    task = TaskDB.get_by_id(task_id, user_id=user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if request.method == "GET":
        return jsonify(list((task.get("colosseum_config") or {}).get("runs") or [])), 200

    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict) or not str(data.get("phase") or "").strip():
        return jsonify({"error": "phase is required"}), 400
    run = {**data}
    run.setdefault("run_id", f"run-{uuid.uuid4().hex[:12]}")
    run.setdefault("created_at", datetime.utcnow().isoformat() + "Z")
    TaskDB.append_colosseum_run(task_id, run, user_id=user_id)
    return jsonify(run), 201


@tasks_bp.route("/api/tasks/<task_id>/approval", methods=["POST"])
def api_task_approval(task_id):
    user_id = getattr(g, "user_id", "")
    task = TaskDB.get_by_id(task_id, user_id=user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if task.get("status") != "human-review":
        return jsonify({"error": "Task is not awaiting human review"}), 409

    data = request.get_json(force=True, silent=True) or {}
    decision = str(data.get("decision") or "").strip().lower()
    reason = str(data.get("comment") or "").strip()
    if decision not in {"approve", "reject"}:
        return jsonify({"error": "decision must be approve or reject"}), 400
    if decision == "reject" and not reason:
        return jsonify({"error": "A rejection comment is required"}), 400

    work_type = str((task.get("colosseum_config") or {}).get("work_type") or "development")
    status = "ready" if decision == "reject" else ("done" if work_type == "research" else "approved")
    label = "approved" if decision == "approve" else "rejected"
    text = f"Human review {label}." + (f"\n\n{reason}" if reason else "")
    _, comments = _comment(task, text, user_id or "user", "user")
    TaskDB.update(task_id, {"status": status, "comments": comments}, user_id=user_id)
    updated = TaskDB.set_colosseum_ready_state(task_id, status in {"ready", "approved"}, user_id=user_id)
    return jsonify(updated), 200



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


@tasks_bp.route("/api/tasks/<task_id>/diff", methods=["GET"])
def api_task_diff(task_id):
    import subprocess
    user_id = getattr(g, "user_id", "")
    task = TaskDB.get_by_id(task_id, user_id=user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    config = task.get("colosseum_config") or {}
    persisted_diff = config.get("diff")
    persisted_files = config.get("files")
    if isinstance(persisted_diff, str) and isinstance(persisted_files, list):
        return jsonify({
            "task_id": task_id,
            "worktree_path": config.get("worktree_path", ""),
            "base_commit": config.get("base_commit"),
            "commit": config.get("commit"),
            "diff": persisted_diff,
            "files": persisted_files,
        })
    worktree_candidates = [
        config.get("worktree_path"),
        os.path.join(
            os.environ.get("SAVANT_COLOSSEUM_WORKTREES_DIR", "~/.savant/colosseum/worktrees"),
            task_id,
        ),
        f"~/.savant-executioner/worktrees/{task_id}",
    ]
    worktree_path = next(
        (
            expanded
            for candidate in worktree_candidates
            if candidate
            for expanded in [os.path.expanduser(str(candidate))]
            if os.path.isdir(expanded)
        ),
        "",
    )

    if not worktree_path:
        return jsonify({"task_id": task_id, "diff": "", "files": [], "error": "Worktree not found"}), 200

    try:
        diff_out = subprocess.check_output(
            ["git", "diff", "HEAD~1..HEAD"],
            cwd=worktree_path,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        try:
            diff_out = subprocess.check_output(
                ["git", "diff"],
                cwd=worktree_path,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as e:
            diff_out = f"Error fetching diff: {e}"

    try:
        files_out = subprocess.check_output(
            ["git", "diff", "--name-status", "HEAD~1..HEAD"],
            cwd=worktree_path,
            stderr=subprocess.STDOUT,
            text=True,
        ).strip().splitlines()
    except Exception:
        files_out = []

    files = []
    for line in files_out:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            files.append({"status": parts[0], "path": parts[1]})

    return jsonify({
        "task_id": task_id,
        "worktree_path": worktree_path,
        "diff": diff_out,
        "files": files,
    })
