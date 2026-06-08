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
import re
from pathlib import Path

# Map of server names to their filenames
SERVERS = {
    "workspace": "server.py",
    "abilities": "abilities_server.py",
    "context": "context_server.py",
    "knowledge": "knowledge_server.py",
    "reminders": "reminders_server.py",
}


def _discover_savant_api_base() -> str | None:
    """Best-effort discovery of the running Savant Flask URL for stdio launches."""
    if os.environ.get("SAVANT_API_BASE"):
        return os.environ["SAVANT_API_BASE"]

    try:
        pgrep = subprocess.run(
            ["pgrep", "-f", r"savant/app\.py|Contents/Resources/savant/app\.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids = [line.strip() for line in pgrep.stdout.splitlines() if line.strip()]
        for pid in reversed(pids):
            lsof = subprocess.run(
                ["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in lsof.stdout.splitlines():
                match = re.search(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", line)
                if match:
                    return f"http://127.0.0.1:{match.group(1)}"
    except Exception:
        pass

    log_path = Path.home() / "Library" / "Application Support" / "savant" / "savant-main.log"
    if not log_path.exists():
        return None

    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-4000:]
    except Exception:
        return None

    patterns = [
        re.compile(r"Flask ready .* on port (\d+)"),
        re.compile(r"Flask port allocated: (\d+)"),
    ]

    for line in reversed(lines):
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                return f"http://127.0.0.1:{match.group(1)}"
    return None

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
