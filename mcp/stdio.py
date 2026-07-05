"""
Unified STDIO entry point for Savant MCP servers.
Used primarily by AI tools that do not support SSE (like Codex).

Usage:
  python3 stdio.py <server_name> [args...]

Example:
  python3 stdio.py workspace
"""

import os
import sys
import subprocess

# Map of server names to their filenames
SERVERS = {
    "workspace": "server.py",
    "abilities": "abilities_server.py",
    "context": "context_server.py",
    "knowledge": "knowledge_server.py",
    "reminders": "reminders_server.py",
}


def _discover_savant_api_base() -> str:
    """Return the Docker-exposed Savant API base unless explicitly overridden."""
    return os.environ.get("SAVANT_API_BASE", "http://127.0.0.1:8090")

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <server_name> [additional args...]", file=sys.stderr)
        print(f"Available servers: {', '.join(SERVERS.keys())}", file=sys.stderr)
        sys.exit(1)

    name = sys.argv[1]
    if name not in SERVERS:
        print(f"Unknown server: {name}", file=sys.stderr)
        print(f"Available servers: {', '.join(SERVERS.keys())}", file=sys.stderr)
        sys.exit(1)

    server_file = SERVERS[name]
    mcp_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(mcp_dir, server_file)

    if not os.path.isfile(script_path):
        print(f"Server file not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    # Call the script with --transport stdio and pass through any extra args
    # Note: we skip the first 2 args (stdio.py and <server_name>)
    args = [script_path, "--transport", "stdio"] + sys.argv[2:]
    env = os.environ.copy()
    api_base = _discover_savant_api_base()
    if api_base:
        env["SAVANT_API_BASE"] = api_base

    # Use execv to replace the current process (on Unix)
    if os.name == "posix":
        try:
            os.execve(sys.executable, [sys.executable] + args, env)
        except Exception as e:
            print(f"Failed to exec: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Fallback for Windows
        process = subprocess.run([sys.executable] + args, env=env)
        sys.exit(process.returncode)

if __name__ == "__main__":
    main()
