
import pytest
import asyncio
import subprocess
import time
import os
import signal
import socket
import sys
import requests
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

pytestmark = pytest.mark.no_db
ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def mcp_server_factory():
    processes = []
    
    def _start_server(script_path, port, transport):
        process = subprocess.Popen(
            [sys.executable, script_path, "--transport", transport, "--port", str(port)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    stderr = process.stderr.read().decode(errors="replace") if process.poll() is not None else ""
                    raise RuntimeError(f"MCP server did not start on port {port}: {stderr}")
                time.sleep(0.05)
        return f"http://127.0.0.1:{port}/{'sse' if transport == 'sse' else 'mcp'}"
    
    yield _start_server
    
    # Teardown
    for p in processes:
        p.terminate()
        p.wait()

@pytest.mark.parametrize("server_name,script_name,port,tool_name", [
    ("workspace", "server.py", 8091, "list_workspaces"),
    ("abilities", "abilities_server.py", 8092, "list_personas"),
    ("context", "context_server.py", 8093, "research"),
    ("knowledge", "knowledge_server.py", 8094, "search"),
    ("reminders", "reminders_server.py", 8095, "list_reminders"),
])
def test_mcp_sse_connection(mcp_server_factory, server_name, script_name, port, tool_name):
    # Test connection via SSE
    script_path = os.path.join(os.path.dirname(__file__), f"../mcp/{script_name}")
    server_url = mcp_server_factory(script_path, port, "sse")
    
    async def verify():
        async with sse_client(server_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert tools
                assert tool_name in [tool.name for tool in tools]

    asyncio.run(verify())


@pytest.mark.parametrize("server_name,script_name,port,tool_name", [
    ("workspace", "server.py", 8191, "list_workspaces"),
    ("abilities", "abilities_server.py", 8192, "list_personas"),
    ("context", "context_server.py", 8193, "research"),
    ("knowledge", "knowledge_server.py", 8194, "search"),
    ("reminders", "reminders_server.py", 8195, "list_reminders"),
])
def test_mcp_streamable_http_connection(mcp_server_factory, server_name, script_name, port, tool_name):
    script_path = os.path.join(os.path.dirname(__file__), f"../mcp/{script_name}")
    server_url = mcp_server_factory(script_path, port, "streamable-http")

    async def verify():
        # The test fixture owns process teardown. Avoid a client-side DELETE
        # teardown request that can wait on a streaming response in MCP 1.x.
        async with streamablehttp_client(server_url, terminate_on_close=False) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool_names = [tool.name for tool in (await session.list_tools()).tools]
                assert tool_name in tool_names

    asyncio.run(verify())
