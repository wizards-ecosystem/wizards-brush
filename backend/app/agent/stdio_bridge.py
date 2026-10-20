"""Relay stdio JSON-RPC to the running app's ``/api/mcp``.

This process is not an MCP server and does not import one. A desktop client
that only speaks stdio (Claude Desktop, including Windows talking to WSL)
runs it; the app that already holds the queue answers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

_DEFAULT_URL = "http://127.0.0.1:8000/api/mcp"


class Bridge:
    """One stdio session forwarded to one Streamable HTTP session."""

    def __init__(self, url: str, token: str, http: httpx.Client) -> None:
        self.url = url
        self.token = token
        self.http = http
        self.session_id = ""
        self.protocol = ""

    def headers(self) -> dict[str, str]:
        sent = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.token:
            sent["X-API-Token"] = self.token
            sent["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            sent["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            sent["MCP-Protocol-Version"] = self.protocol
        return sent

    def forward(self, message: dict[str, Any]) -> list[str]:
        """POST one JSON-RPC message. Each returned string is one stdout line."""
        try:
            response = self.http.post(self.url, headers=self.headers(), json=message)
        except httpx.HTTPError as exc:
            return self._error(message, f"brush is not reachable: {exc}")
        session = response.headers.get("mcp-session-id")
        if session:
            self.session_id = session
        if response.status_code == 401:
            return self._error(message, "unauthorized")
        if response.status_code == 202:
            return []
        if response.status_code >= 400:
            detail = response.text.strip()[:500] or f"HTTP {response.status_code}"
            return self._error(message, detail)
        ctype = response.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            lines = sse_payloads(response.text)
        else:
            body = response.text.strip()
            lines = [body] if body else []
        self._note_protocol(message, lines)
        return lines

    def _note_protocol(self, message: dict[str, Any], lines: list[str]) -> None:
        if message.get("method") != "initialize":
            return
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            version = (payload.get("result") or {}).get("protocolVersion")
            if isinstance(version, str) and version:
                self.protocol = version
                return

    def _error(self, message: dict[str, Any], text: str) -> list[str]:
        if "id" not in message:
            sys.stderr.write(text + "\n")
            return []
        return [json.dumps({
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32000, "message": text},
        })]


def sse_payloads(text: str) -> list[str]:
    """JSON payloads from an SSE body, one per event. Comments are skipped."""
    payloads: list[str] = []
    data: list[str] = []

    def flush() -> None:
        if data:
            payloads.append("\n".join(data))
            data.clear()

    for line in text.splitlines():
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
    flush()
    return [item for item in payloads if item and item != "[DONE]"]


def parser() -> argparse.ArgumentParser:
    args = argparse.ArgumentParser(description="Relay stdio MCP to a running Brush app.")
    args.add_argument(
        "--url",
        default=os.environ.get("BRUSH_URL", _DEFAULT_URL),
        help=f"Streamable HTTP endpoint (default {_DEFAULT_URL}, or BRUSH_URL).",
    )
    return args


def serve(bridge: Bridge, stdin: Any, stdout: Any) -> None:
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write("ignored a line that is not JSON\n")
            continue
        if not isinstance(message, dict):
            sys.stderr.write("ignored a JSON value that is not an object\n")
            continue
        for out in bridge.forward(message):
            stdout.write(out + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    token = os.environ.get("BRUSH_API_TOKEN", "")
    # Tools may wait ~55s. A client timeout must not cut that short.
    with httpx.Client(timeout=None) as http:
        serve(Bridge(args.url, token, http), sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
