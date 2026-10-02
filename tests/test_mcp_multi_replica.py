"""Tests for MCP multi-replica scalability without sticky sessions."""

import asyncio
import os
import subprocess
import sys
import time
import httpx2
import pytest

pytestmark = pytest.mark.no_db


def test_mcp_stateless_multi_replica_dispatch():
    """Verify that independent MCP server instances (replicas) can serve the same client session."""
    port1 = 9211
    port2 = 9212

    p1 = subprocess.Popen(
        [sys.executable, "mcp/abilities_server.py", "--transport", "streamable-http", "--port", str(port1), "--stateless"]
    )
    p2 = subprocess.Popen(
        [sys.executable, "mcp/abilities_server.py", "--transport", "streamable-http", "--port", str(port2), "--stateless"]
    )
    time.sleep(1.5)

    headers = {
        "Accept": "application/json, text/event-stream",
        "X-API-Key": "sk-ahmed-savant-001",
        "Mcp-Session-Id": "test-k8s-replica-session",
    }

    async def verify():
        async with httpx2.AsyncClient() as client:
            # 1. Initialize on Replica 1
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-replica", "version": "1.0"},
                },
            }
            r1 = await client.post(f"http://127.0.0.1:{port1}/mcp", json=init_req, headers=headers)
            assert r1.status_code == 200

            # 2. Tools list on Replica 2 (different replica!)
            list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            r2 = await client.post(f"http://127.0.0.1:{port2}/mcp", json=list_req, headers=headers)
            assert r2.status_code == 200
            assert "list_personas" in r2.text

    try:
        asyncio.run(verify())
    finally:
        p1.terminate()
        p2.terminate()
        p1.wait()
        p2.wait()


def test_mcp_stateless_default_without_flags():
    """Verify that MCP server defaults to stateless without needing any special flags."""
    port1 = 9213
    port2 = 9214

    # Run WITHOUT passing --stateless or setting any env vars
    p1 = subprocess.Popen(
        [sys.executable, "mcp/abilities_server.py", "--transport", "streamable-http", "--port", str(port1)]
    )
    p2 = subprocess.Popen(
        [sys.executable, "mcp/abilities_server.py", "--transport", "streamable-http", "--port", str(port2)]
    )
    time.sleep(1.5)

    headers = {
        "Accept": "application/json, text/event-stream",
        "X-API-Key": "sk-ahmed-savant-001",
        "Mcp-Session-Id": "test-default-session-id-123",
    }

    async def verify():
        async with httpx2.AsyncClient() as client:
            # 1. Initialize on Replica 1
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"},
                },
            }
            r1 = await client.post(f"http://127.0.0.1:{port1}/mcp", json=init_req, headers=headers)
            assert r1.status_code == 200

            # 2. Tools list on Replica 2 (no session not found error!)
            list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            r2 = await client.post(f"http://127.0.0.1:{port2}/mcp", json=list_req, headers=headers)
            assert r2.status_code == 200
            assert "list_personas" in r2.text

    try:
        asyncio.run(verify())
    finally:
        p1.terminate()
        p2.terminate()
        p1.wait()
        p2.wait()

