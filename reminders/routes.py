"""Flask Blueprint for Reminders REST API. All routes under /api/reminders/*."""
import uuid
from datetime import datetime, timezone
from flask import Blueprint, g, jsonify, request
from db.reminders import ReminderDB

reminders_bp = Blueprint("reminders", __name__)


def _reminder_id():
    return f"rem-{uuid.uuid4().hex[:12]}"


@reminders_bp.route("/api/reminders", methods=["GET"])
def list_reminders():
    status = request.args.get("status")
    reminders = ReminderDB.list_all(status=status, user_id=g.user_id)
    return jsonify(reminders)


@reminders_bp.route("/api/reminders/due-today", methods=["GET"])
def due_today():
    reminders = ReminderDB.list_due_today(user_id=g.user_id)
    return jsonify(reminders)


@reminders_bp.route("/api/reminders/due-soon", methods=["GET"])
def due_soon():
    within_hrs = int(request.args.get("within_hrs", 1))
    reminders = ReminderDB.list_due_soon(within_hrs=within_hrs, user_id=g.user_id)
    return jsonify(reminders)


@reminders_bp.route("/api/reminders/overdue", methods=["GET"])
def overdue():
    reminders = ReminderDB.list_overdue(user_id=g.user_id)
    return jsonify(reminders)


@reminders_bp.route("/api/reminders", methods=["POST"])
def create_reminder():
    data = request.get_json(force=True) or {}
    if not data.get("title"):
        return jsonify({"error": "title is required"}), 400
    if not data.get("due_date"):
        return jsonify({"error": "due_date is required"}), 400
    now = datetime.now(timezone.utc).isoformat()
    reminder = ReminderDB.create({
        "reminder_id": data.get("reminder_id") or _reminder_id(),
        "title": data["title"],
        "description": data.get("description", ""),
        "priority": data.get("priority", "medium"),
        "status": "pending",
        "start_date": data.get("start_date", now),
        "due_date": data["due_date"],
        "remind_before_hrs": int(data.get("remind_before_hrs", 1)),
        "created_at": now,
        "updated_at": now,
        "user_id": g.user_id,
    })
    return jsonify(reminder), 201


@reminders_bp.route("/api/reminders/<reminder_id>", methods=["GET"])
def get_reminder(reminder_id):
    reminder = ReminderDB.get_by_id(reminder_id, user_id=g.user_id)
    if not reminder:
        return jsonify({"error": "not found"}), 404
    return jsonify(reminder)


@reminders_bp.route("/api/reminders/<reminder_id>", methods=["PUT"])
def update_reminder(reminder_id):
    reminder = ReminderDB.get_by_id(reminder_id, user_id=g.user_id)
    if not reminder:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True) or {}
    updated = ReminderDB.update(reminder_id, data, user_id=g.user_id)
    return jsonify(updated)


@reminders_bp.route("/api/reminders/<reminder_id>/complete", methods=["POST"])
def complete_reminder(reminder_id):
    reminder = ReminderDB.get_by_id(reminder_id, user_id=g.user_id)
    if not reminder:
        return jsonify({"error": "not found"}), 404
    updated = ReminderDB.complete(reminder_id, user_id=g.user_id)
    return jsonify(updated)


@reminders_bp.route("/api/reminders/<reminder_id>/dismiss", methods=["POST"])
def dismiss_reminder(reminder_id):
    reminder = ReminderDB.get_by_id(reminder_id, user_id=g.user_id)
    if not reminder:
        return jsonify({"error": "not found"}), 404
    updated = ReminderDB.dismiss(reminder_id, user_id=g.user_id)
    return jsonify(updated)


@reminders_bp.route("/api/reminders/<reminder_id>", methods=["DELETE"])
def delete_reminder(reminder_id):
    ok = ReminderDB.delete(reminder_id, user_id=g.user_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": reminder_id})
