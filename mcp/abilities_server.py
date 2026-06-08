"""
savant-abilities MCP Server

Thin MCP bridge to the Savant Dashboard Flask API (/api/abilities/*).
Runs as SSE on port 8092.  Same tool signatures as the standalone
savant-abilities package for backward compatibility, plus new CRUD tools.

Asset types managed:
  persona   — AI agent personas (system prompt base)
  rule      — Reusable rules injected into resolved prompts
  policy    — Behavioural policies and constraints
  style     — Output/code style guidelines
  repo      — Repository overlays (repo-specific rule overrides)

Tool groups:
  Resolution  — resolve_abilities (persona + tags + optional repo → prompt), validate_store
  Listing     — list_personas, list_rules, list_policies, list_repos
  CRUD        — read_asset, create_asset, update_asset
  Learning    — learn (append to an asset's ## Learned section)
"""

import argparse
import base64
import json
import logging
import os
import sys
from typing import Any, Optional

import requests
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.environ.get("SAVANT_API_BASE", "http://localhost:8090")
REQUEST_TIMEOUT = 60

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("savant-abilities")

from auth import auth_headers, install_header_capture

# ---------------------------------------------------------------------------
# Entry point args
# ---------------------------------------------------------------------------

_parser = argparse.ArgumentParser(description="savant-abilities MCP server")
_parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
_parser.add_argument("--port", type=int, default=8092)
_parser.add_argument("--host", default="127.0.0.1")
_args, _ = _parser.parse_known_args()

mcp = FastMCP(
    "savant-abilities",
    instructions=(
        "Prompt asset management — resolve personas, rules, policies, styles, and repo overlays into deterministic prompts. "
        "Asset types: persona (AI agent base), rule (reusable injected rules), policy (behavioural constraints), "
        "style (output/code style), repo (repo-specific overrides). "
        "Resolution: resolve_abilities(persona, tags, repo_id) → compiled prompt; validate_store() checks schema. "
        "Listing: list_personas, list_rules, list_policies, list_repos. "
        "CRUD: read_asset(asset_id), create_asset(...), update_asset(asset_id, ...). "
        "Learning: learn(asset_id, content) appends knowledge to an asset's ## Learned section. "
        "Migration: export_abilities() returns a base64 zip of the whole store; import_abilities(content_b64) installs it on another instance (overwrite on conflict)."
    ),
    host=_args.host,
    port=_args.port,

)

install_header_capture(mcp)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, **kwargs) -> dict | list:
    url = f"{API_BASE}{path}"
    hdrs = kwargs.pop("headers", {})
    hdrs.update(auth_headers())
    try:
        resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, headers=hdrs, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        raise RuntimeError(
            f"Dashboard app not running at {API_BASE}. "
            "Start it with: npm run dev (or docker compose up -d)"
        )
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        raise RuntimeError(f"API error {e.response.status_code}: {body}")

# ---------------------------------------------------------------------------
# Tools — backward compatible with v1 + new CRUD tools
# ---------------------------------------------------------------------------

@mcp.tool()
def resolve_abilities(
    persona: str,
    tags: list[str] = [],
    repo_id: str | None = None,
    trace: bool | None = False,
) -> dict[str, Any]:
    """Resolve persona + tagged rules (+ optional repo overlay) into a deterministic prompt"""
    payload: dict[str, Any] = {"persona": persona, "tags": tags or []}
    if repo_id:
        payload["repo_id"] = repo_id
    if trace:
        payload["trace"] = True
    return _api("POST", "/api/abilities/resolve", json=payload)


@mcp.tool()
def validate_store() -> dict[str, Any]:
    """Validate abilities store (schema and include graph)"""
    return _api("GET", "/api/abilities/validate")


@mcp.tool()
def list_personas() -> dict[str, Any]:
    """List available personas (name and path)"""
    data = _api("GET", "/api/abilities/assets")
    items = data.get("persona", [])
    return {"items": [{"name": a.get("name") or a["id"], "path": a.get("path", "")} for a in items]}


@mcp.tool()
def list_repos() -> dict[str, Any]:
    """List available repos (name and path)"""
    data = _api("GET", "/api/abilities/assets")
    items = data.get("repo", [])
    return {"items": [{"name": a.get("name") or a["id"], "path": a.get("path", "")} for a in items]}


@mcp.tool()
def list_rules() -> dict[str, Any]:
    """List available rules (name and path)"""
    data = _api("GET", "/api/abilities/assets")
    items = data.get("rule", [])
    return {"items": [{"name": a.get("name") or a["id"], "path": a.get("path", "")} for a in items]}


@mcp.tool()
def list_policies() -> dict[str, Any]:
    """List available policies and styles (name and path)"""
    data = _api("GET", "/api/abilities/assets")
    policies = data.get("policy", [])
    styles = data.get("style", [])
    items = policies + styles
    return {"items": [{"name": a.get("name") or a["id"], "path": a.get("path", "")} for a in items]}


@mcp.tool()
def learn(asset_id: str, content: str) -> dict[str, Any]:
    """Append knowledge to an asset's ## Learned section"""
    return _api("POST", "/api/abilities/learn", json={"asset_id": asset_id, "content": content})


@mcp.tool()
def read_asset(asset_id: str) -> dict[str, Any]:
    """Read a specific asset by ID (returns frontmatter + body)"""
    return _api("GET", f"/api/abilities/assets/{asset_id}")


@mcp.tool()
def create_asset(
    id: str,
    type: str,
    tags: list[str],
    priority: int,
    body: str = "",
    includes: list[str] | None = None,
    name: str | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new ability asset"""
    payload: dict[str, Any] = {"id": id, "type": type, "tags": tags, "priority": priority, "body": body}
    if includes:
        payload["includes"] = includes
    if name:
        payload["name"] = name
    if aliases:
        payload["aliases"] = aliases
    return _api("POST", "/api/abilities/assets", json=payload)


@mcp.tool()
def update_asset(
    asset_id: str,
    tags: list[str] | None = None,
    priority: int | None = None,
    body: str | None = None,
    includes: list[str] | None = None,
    name: str | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Update an existing ability asset"""
    payload: dict[str, Any] = {}
    if tags is not None:
        payload["tags"] = tags
    if priority is not None:
        payload["priority"] = priority
    if body is not None:
        payload["body"] = body
    if includes is not None:
        payload["includes"] = includes
    if name is not None:
        payload["name"] = name
    if aliases is not None:
        payload["aliases"] = aliases
    return _api("PUT", f"/api/abilities/assets/{asset_id}", json=payload)


@mcp.tool()
def export_abilities() -> dict[str, Any]:
    """Export all abilities (personas, rules, policies, styles, repos) as a base64-encoded zip archive. The same archive can be re-imported on another instance via import_abilities()."""
    url = f"{API_BASE}/api/abilities/export"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=auth_headers())
        resp.raise_for_status()
    except requests.ConnectionError:
        raise RuntimeError(f"Dashboard app not running at {API_BASE}.")
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        raise RuntimeError(f"API error {e.response.status_code}: {body}")
    filename = "savant-abilities.zip"
    cd = resp.headers.get("Content-Disposition", "")
    if "filename=" in cd:
        filename = cd.split("filename=", 1)[1].strip().strip('"')
    return {
        "filename": filename,
        "count": int(resp.headers.get("X-Abilities-Count", "0") or 0),
        "size_bytes": len(resp.content),
        "content_b64": base64.b64encode(resp.content).decode("ascii"),
    }


@mcp.tool()
def import_abilities(content_b64: str) -> dict[str, Any]:
    """Import abilities from a base64-encoded zip archive produced by export_abilities(). Existing assets with the same id are overwritten."""
    try:
        blob = base64.b64decode(content_b64, validate=True)
    except Exception as e:
        raise RuntimeError(f"invalid base64 content: {e}")
    url = f"{API_BASE}/api/abilities/import"
    hdrs = {"Content-Type": "application/zip"}
    hdrs.update(auth_headers())
    try:
        resp = requests.post(url, data=blob, timeout=REQUEST_TIMEOUT, headers=hdrs)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        raise RuntimeError(f"Dashboard app not running at {API_BASE}.")
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        raise RuntimeError(f"API error {e.response.status_code}: {body}")


# ---------------------------------------------------------------------------
# Patch: catch ClosedResourceError in SSE transport
# ---------------------------------------------------------------------------
def _patch_sse_transport():
    try:
        from mcp.server.sse import SseServerTransport
        from anyio import ClosedResourceError
        original_handle = SseServerTransport.handle_post_message

        async def _safe_handle(self, scope, receive, send):
            try:
                await original_handle(self, scope, receive, send)
            except Exception as e:
                if "ClosedResourceError" in type(e).__name__ or "ClosedResource" in str(e):
                    log.debug("SSE client disconnected before response sent (harmless)")
                else:
                    raise

        SseServerTransport.handle_post_message = _safe_handle
    except Exception as e:
        log.warning(f"Could not apply SSE patch: {e}")

_patch_sse_transport()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _args.transport in ("sse", "streamable-http"):
        log.info(f"Starting savant-abilities MCP ({_args.transport}) on {_args.host}:{_args.port}")
    mcp.run(transport=_args.transport)
