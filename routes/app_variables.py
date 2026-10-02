"""API routes for managing app variables."""

from __future__ import annotations

import re
from flask import Blueprint, jsonify, request
from db.app_variables import AppVariablesDB

app_variables_bp = Blueprint("app_variables", __name__)

_SENSITIVE_KEY_PATTERN = re.compile(r"(TOKEN|SECRET|PASSWORD|KEY|AUTH)", re.IGNORECASE)


def _mask_value(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "********"
    return f"{val[:4]}...{val[-4:]}"


@app_variables_bp.route("/api/app-variables", methods=["GET"])
@app_variables_bp.route("/api/app_variables", methods=["GET"])
def list_app_variables():
    """Retrieve stored and effective app variables."""
    raw = request.args.get("raw", "").lower() in ("1", "true", "yes")
    all_vars = AppVariablesDB.list_all()

    display_vars = {}
    for k, v in all_vars.items():
        if not raw and _SENSITIVE_KEY_PATTERN.search(k):
            display_vars[k] = _mask_value(v)
        else:
            display_vars[k] = v

    # Standard tracked keys
    effective = {
        "GITHUB_TOKEN": AppVariablesDB.get_effective_info("GITHUB_TOKEN"),
        "GITLAB_TOKEN": AppVariablesDB.get_effective_info("GITLAB_TOKEN"),
    }

    return jsonify({
        "variables": display_vars,
        "effective": effective,
    })


@app_variables_bp.route("/api/app-variables/<key>", methods=["GET"])
@app_variables_bp.route("/api/app_variables/<key>", methods=["GET"])
def get_app_variable(key: str):
    """Retrieve a single variable by key."""
    key = key.strip()
    db_val = AppVariablesDB.get(key)
    effective_info = AppVariablesDB.get_effective_info(key)
    raw = request.args.get("raw", "").lower() in ("1", "true", "yes")

    if db_val is None and not effective_info["is_set"]:
        return jsonify({"error": f"Variable '{key}' not found"}), 404

    val = db_val if db_val is not None else ""
    if not raw and _SENSITIVE_KEY_PATTERN.search(key):
        display_val = _mask_value(val)
    else:
        display_val = val

    return jsonify({
        "key": key,
        "value": display_val,
        "effective_info": effective_info,
    })


@app_variables_bp.route("/api/app-variables", methods=["POST"])
@app_variables_bp.route("/api/app_variables", methods=["POST"])
def set_app_variables():
    """Set one or more application variables."""
    data = request.get_json(force=True, silent=True) or {}

    # Support batch format: {"variables": {"GITHUB_TOKEN": "...", "GITLAB_TOKEN": "..."}}
    if "variables" in data and isinstance(data["variables"], dict):
        updated = {}
        for k, v in data["variables"].items():
            clean_k = str(k).strip()
            clean_v = str(v).strip()
            if clean_k:
                AppVariablesDB.set(clean_k, clean_v)
                updated[clean_k] = _mask_value(clean_v) if _SENSITIVE_KEY_PATTERN.search(clean_k) else clean_v
        return jsonify({"status": "saved", "updated": updated}), 200

    # Support single format: {"key": "GITHUB_TOKEN", "value": "..."}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400

    value = str(data.get("value", "")).strip()
    AppVariablesDB.set(key, value)

    return jsonify({
        "status": "saved",
        "key": key,
        "value": _mask_value(value) if _SENSITIVE_KEY_PATTERN.search(key) else value,
    }), 200


@app_variables_bp.route("/api/app-variables/<key>", methods=["PUT"])
@app_variables_bp.route("/api/app_variables/<key>", methods=["PUT"])
def put_app_variable(key: str):
    """Update a single variable."""
    key = key.strip()
    if not key:
        return jsonify({"error": "key is required"}), 400

    data = request.get_json(force=True, silent=True) or {}
    value = str(data.get("value", "")).strip()
    AppVariablesDB.set(key, value)

    return jsonify({
        "status": "saved",
        "key": key,
        "value": _mask_value(value) if _SENSITIVE_KEY_PATTERN.search(key) else value,
    }), 200


@app_variables_bp.route("/api/app-variables/<key>", methods=["DELETE"])
@app_variables_bp.route("/api/app_variables/<key>", methods=["DELETE"])
def delete_app_variable(key: str):
    """Delete a variable by key from database."""
    key = key.strip()
    deleted = AppVariablesDB.delete(key)
    return jsonify({
        "status": "deleted" if deleted else "not_found",
        "key": key,
    }), 200 if deleted else 404
