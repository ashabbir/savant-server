"""Jira Tickets & Merge Requests Routes Blueprint for Savant Server."""

import uuid

from flask import Blueprint, g, jsonify, request
from db.jira_tickets import JiraTicketDB
from db.merge_requests import MergeRequestDB

jira_mr_bp = Blueprint("jira_mr", __name__)


@jira_mr_bp.route("/api/jira-tickets", methods=["GET", "POST"])
def api_jira_tickets():
    user_id = getattr(g, "user_id", "")
    if request.method == "GET":
        workspace_id = request.args.get("workspace_id")
        tickets = JiraTicketDB.list_all(user_id=user_id, workspace_id=workspace_id)
        return jsonify(tickets)

    data = request.get_json(force=True, silent=True) or {}
    ticket_key = (data.get("ticket_key") or data.get("key") or "").strip()
    if not ticket_key:
        return jsonify({"error": "ticket_key is required"}), 400

    created = JiraTicketDB.create({
        "ticket_key": ticket_key,
        "title": (data.get("title") or data.get("summary") or "").strip(),
        "status": data.get("status", "open"),
        "priority": data.get("priority", "medium"),
        "workspace_id": data.get("workspace_id"),
        "user_id": user_id,
    })
    return jsonify(created), 201


@jira_mr_bp.route("/api/jira-tickets/<ticket_id>", methods=["GET", "PUT", "DELETE"])
def api_jira_ticket_detail(ticket_id):
    user_id = getattr(g, "user_id", "")
    ticket = JiraTicketDB.get_by_id(ticket_id, user_id=user_id)
    if not ticket:
        return jsonify({"error": "Jira ticket not found"}), 404

    if request.method == "GET":
        return jsonify(ticket)

    if request.method == "DELETE":
        JiraTicketDB.delete(ticket_id, user_id=user_id)
        return jsonify({"status": "deleted"}), 200

    data = request.get_json(force=True, silent=True) or {}
    updated = JiraTicketDB.update(ticket_id, data, user_id=user_id)
    return jsonify(updated)


@jira_mr_bp.route("/api/jira-tickets/<ticket_id>/notes", methods=["POST"])
def api_jira_ticket_add_note(ticket_id):
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or data.get("content") or "").strip()
    if not text:
        return jsonify({"error": "Note text is required"}), 400

    note = JiraTicketDB.add_note(ticket_id, text, session_id=data.get("session_id"), user_id=user_id)
    if not note:
        return jsonify({"error": "Ticket not found"}), 404
    return jsonify(note), 201


@jira_mr_bp.route("/api/merge-requests", methods=["GET", "POST"])
def api_merge_requests():
    user_id = getattr(g, "user_id", "")
    if request.method == "GET":
        workspace_id = request.args.get("workspace_id")
        if not workspace_id:
            return jsonify({"error": "workspace_id parameter is required"}), 400
        mrs = MergeRequestDB.list_all(user_id=user_id, workspace_id=workspace_id)
        return jsonify(mrs)

    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    url = (data.get("url") or "").strip()
    if not title and not url:
        return jsonify({"error": "title or url is required"}), 400

    created = MergeRequestDB.create({
        "mr_id": data.get("mr_id") or f"mr-{uuid.uuid4().hex[:12]}",
        "title": title or url,
        "url": url,
        "status": data.get("status", "open"),
        "workspace_id": data.get("workspace_id"),
        "author": data.get("author", ""),
        "user_id": user_id,
    })
    return jsonify(created), 201


@jira_mr_bp.route("/api/merge-requests/<mr_id>", methods=["GET", "PUT", "DELETE"])
def api_merge_request_detail(mr_id):
    user_id = getattr(g, "user_id", "")
    mr = MergeRequestDB.get_by_id(mr_id, user_id=user_id)
    if not mr:
        return jsonify({"error": "Merge request not found"}), 404

    if request.method == "GET":
        return jsonify(mr)

    if request.method == "DELETE":
        MergeRequestDB.delete(mr_id, user_id=user_id)
        return jsonify({"status": "deleted"}), 200

    data = request.get_json(force=True, silent=True) or {}
    updated = MergeRequestDB.update(mr_id, data, user_id=user_id)
    return jsonify(updated)


@jira_mr_bp.route("/api/merge-requests/<mr_id>/notes", methods=["POST"])
def api_merge_request_add_note(mr_id):
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or data.get("content") or "").strip()
    if not text:
        return jsonify({"error": "Note text is required"}), 400

    note = MergeRequestDB.add_note(mr_id, text, session_id=data.get("session_id"), user_id=user_id)
    if not note:
        return jsonify({"error": "Merge request not found"}), 404
    return jsonify(note), 201


@jira_mr_bp.route("/api/all-mrs", methods=["GET"])
def api_all_mrs():
    user_id = getattr(g, "user_id", "")
    mrs = MergeRequestDB.list_all(user_id=user_id)
    return jsonify(mrs)


@jira_mr_bp.route("/api/all-jira", methods=["GET"])
def api_all_jira():
    user_id = getattr(g, "user_id", "")
    tickets = JiraTicketDB.list_all(user_id=user_id)
    return jsonify(tickets)
