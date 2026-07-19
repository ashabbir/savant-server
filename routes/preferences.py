"""Preferences, Models, and LLM Provider Routes Blueprint for Savant Server."""

import os
import json
import sys
import platform
from flask import Blueprint, jsonify, request

preferences_bp = Blueprint("preferences", __name__)

_PREFERENCES_FILE = os.path.expanduser("~/.savant/preferences.json")
_PROVIDERS_FILE = os.path.expanduser("~/.savant/llm_providers.json")
_MODELS_FILE = os.path.expanduser("~/.savant/models.json")
_LLM_CONFIG_FILE = os.path.expanduser("~/.savant/llm_config.json")


def _read_json_file(path: str, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_user_preference(key: str, default=None):
    prefs = _read_json_file(_PREFERENCES_FILE, default={})
    return prefs.get(key, default)


@preferences_bp.route("/api/preferences", methods=["GET", "POST"])
def api_preferences():
    if request.method == "GET":
        prefs = _read_json_file(_PREFERENCES_FILE, default={
            "theme": "dark",
            "name": "Operator",
            "work_week": [1, 2, 3, 4, 5],
            "enabled_providers": ["savant", "claude", "codex", "gemini"],
            "terminal": {
                "externalTerminal": "auto",
                "shell": "/bin/zsh",
                "fontSize": 13,
                "scrollback": 5000,
                "customCommand": "",
            },
        })
        return jsonify(prefs)

    data = request.get_json(force=True, silent=True) or {}
    _write_json_file(_PREFERENCES_FILE, data)
    return jsonify(data)


@preferences_bp.route("/api/environment", methods=["GET"])
def api_environment():
    return jsonify({
        "platform": platform.system(),
        "release": platform.release(),
        "python_version": sys.version,
        "environment": os.environ.get("SAVANT_ENV", "development"),
        "api_only": os.environ.get("SAVANT_API_ONLY", "false").lower() in ("1", "true", "yes"),
    })


@preferences_bp.route("/api/llm-providers", methods=["GET", "POST"])
def api_llm_providers():
    providers = _read_json_file(_PROVIDERS_FILE, default=[])
    if request.method == "GET":
        return jsonify(providers)

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Provider name is required"}), 400

    provider_id = (data.get("id") or name.lower().replace(" ", "-")).strip()
    data["id"] = provider_id
    providers.append(data)
    _write_json_file(_PROVIDERS_FILE, providers)
    return jsonify(data), 201


@preferences_bp.route("/api/llm-providers/<provider_id>", methods=["PUT", "DELETE"])
def api_llm_provider_detail(provider_id):
    providers = _read_json_file(_PROVIDERS_FILE, default=[])
    matching = [p for p in providers if p.get("id") == provider_id]
    if not matching:
        return jsonify({"error": "Provider not found"}), 404

    if request.method == "DELETE":
        providers = [p for p in providers if p.get("id") != provider_id]
        _write_json_file(_PROVIDERS_FILE, providers)
        return jsonify({"status": "deleted"}), 200

    data = request.get_json(force=True, silent=True) or {}
    for i, p in enumerate(providers):
        if p.get("id") == provider_id:
            providers[i].update(data)
            _write_json_file(_PROVIDERS_FILE, providers)
            return jsonify(providers[i])

    return jsonify({"error": "Provider not found"}), 404


@preferences_bp.route("/api/models", methods=["GET", "POST"])
def api_models():
    models = _read_json_file(_MODELS_FILE, default=[])
    if request.method == "GET":
        return jsonify(models)

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Model name is required"}), 400

    model_id = (data.get("id") or name.lower().replace(" ", "-")).strip()
    data["id"] = model_id
    models.append(data)
    _write_json_file(_MODELS_FILE, models)
    return jsonify(data), 201


@preferences_bp.route("/api/models/<model_id>", methods=["PUT", "DELETE"])
def api_model_detail(model_id):
    models = _read_json_file(_MODELS_FILE, default=[])
    matching = [m for m in models if m.get("id") == model_id]
    if not matching:
        return jsonify({"error": "Model not found"}), 404

    if request.method == "DELETE":
        models = [m for m in models if m.get("id") != model_id]
        _write_json_file(_MODELS_FILE, models)
        return jsonify({"status": "deleted"}), 200

    data = request.get_json(force=True, silent=True) or {}
    for i, m in enumerate(models):
        if m.get("id") == model_id:
            models[i].update(data)
            _write_json_file(_MODELS_FILE, models)
            return jsonify(models[i])

    return jsonify({"error": "Model not found"}), 404


@preferences_bp.route("/api/llm-config", methods=["GET", "POST"])
def api_llm_config():
    if request.method == "GET":
        config = _read_json_file(_LLM_CONFIG_FILE, default={})
        return jsonify(config)

    data = request.get_json(force=True, silent=True) or {}
    _write_json_file(_LLM_CONFIG_FILE, data)
    return jsonify(data)


@preferences_bp.route("/version", methods=["GET"])
@preferences_bp.route("/api/version", methods=["GET"])
def api_version():
    return jsonify({
        "version": "1.0.0",
        "app": "savant-server",
        "status": "ok",
    })


@preferences_bp.route("/api/utils/markdown", methods=["POST"])
def api_markdown_render():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    return jsonify({"html": f"<p>{text}</p>"})
