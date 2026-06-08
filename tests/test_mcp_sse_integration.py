
import pytest
import subprocess
import time
import os
import signal
import sys
import requests
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

@pytest.fixture
def mcp_server_factory():
    processes = []
    
    def _start_server(script_path, port):
        process = subprocess.Popen(
            [sys.executable, script_path, "--transport", "sse", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        time.sleep(5)
        return f"http://127.0.0.1:{port}/sse"
    
    yield _start_server
    
    # Teardown
    for p in processes:
        p.terminate()
        p.wait()

@pytest.mark.asyncio
@pytest.mark.parametrize("server_name,script_name,port,tool_name", [
    ("workspace", "server.py", 8091, "list_workspaces"),
    ("abilities", "abilities_server.py", 8092, "list_personas"),
    ("context", "context_server.py", 8093, "code_search"),
    ("knowledge", "knowledge_server.py", 8094, "search"),
    ("reminders", "reminders_server.py", 8095, "list_reminders"),
])
async def test_mcp_sse_connection(mcp_server_factory, server_name, script_name, port, tool_name):
    # Test connection via SSE
    script_path = os.path.join(os.path.dirname(__file__), f"../mcp/{script_name}")
    server_url = mcp_server_factory(script_path, port)
    
    async with sse_client(server_url) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize session
            await session.initialize()
            
            # List tools
            list_tools_result = await session.list_tools()
            tools = list_tools_result.tools
            assert len(tools) > 0
            
            # Check if one of the known tools exists
            tool_names = [t.name for t in tools]
            assert tool_name in tool_names

