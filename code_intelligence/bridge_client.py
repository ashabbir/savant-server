"""Bounded NDJSON RPC client for the private CodeGraph Unix socket."""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

from .provider import CodeIntelligenceError, ErrorCategory


_ERRORS = {category.name: category for category in ErrorCategory}


DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CodeGraphBridgeClient:
    READ_CHUNK_SIZE = 65536

    def __init__(self, socket_path: str | Path, *, timeout: float = DEFAULT_TIMEOUT, max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def _prepare_payload(self, request_id: str, operation: str, repo_id: str | None, params: dict | None) -> bytes:
        request = {"id": request_id, "op": operation, "repo_id": repo_id, "params": params or {}}
        return json.dumps(request, separators=(",", ":")).encode() + b"\n"

    def _create_connection(self, timeout: float | None) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout if timeout is None else timeout)
        connection.connect(self.socket_path)
        return connection

    def _call_bridge(self, payload: bytes, timeout: float | None) -> bytes:
        try:
            with self._create_connection(timeout) as connection:
                connection.sendall(payload)
                return self._read_line(connection)
        except (TimeoutError, socket.timeout) as error:
            raise CodeIntelligenceError(ErrorCategory.TIMEOUT, "CodeGraph bridge timed out", retryable=True) from error
        except OSError as error:
            raise CodeIntelligenceError(ErrorCategory.ENGINE_UNAVAILABLE, "CodeGraph bridge unavailable", retryable=True) from error

    def _decode_result(self, response: bytes, request_id: str) -> any:
        try:
            envelope = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CodeIntelligenceError(ErrorCategory.INTERNAL, "CodeGraph bridge returned invalid JSON") from error
        if envelope.get("id") != request_id:
            raise CodeIntelligenceError(ErrorCategory.INTERNAL, "CodeGraph bridge response ID mismatch")
        if not envelope.get("ok"):
            detail = envelope.get("error") or {}
            category = _ERRORS.get(str(detail.get("code", "INTERNAL")).upper(), ErrorCategory.INTERNAL)
            raise CodeIntelligenceError(category, str(detail.get("message") or "CodeGraph bridge error"), retryable=bool(detail.get("retryable")))
        return envelope.get("result")

    def call(self, operation: str, *, repo_id: str | None = None, params: dict | None = None,
             request_id: str | None = None, timeout: float | None = None) -> any:
        request_id = request_id or uuid.uuid4().hex
        payload = self._prepare_payload(request_id, operation, repo_id, params)
        response = self._call_bridge(payload, timeout)
        return self._decode_result(response, request_id)

    def cancel(self, request_id: str) -> any:
        """Cancel an in-flight bridge operation by its caller-supplied ID."""
        return self.call("cancel", params={"request_id": request_id})

    def _read_line(self, connection: socket.socket) -> bytes:
        chunks = bytearray()
        while True:
            max_read = self.max_response_bytes + 1 - len(chunks)
            chunk = connection.recv(min(self.READ_CHUNK_SIZE, max_read))
            if not chunk:
                raise CodeIntelligenceError(ErrorCategory.ENGINE_UNAVAILABLE, "CodeGraph bridge closed the connection", retryable=True)
            chunks.extend(chunk)
            newline = chunks.find(b"\n")
            if newline >= 0:
                return bytes(chunks[:newline])
            if len(chunks) > self.max_response_bytes:
                raise CodeIntelligenceError(ErrorCategory.INTERNAL, "CodeGraph bridge response exceeded size cap")


# Concise public name retained for runtime composition and future transports.
BridgeClient = CodeGraphBridgeClient
