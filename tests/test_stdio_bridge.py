"""The stdio bridge forwards JSON-RPC and does not start an MCP server."""
from __future__ import annotations

import json
import subprocess
import sys

import httpx

from backend.app.agent.stdio_bridge import Bridge, sse_payloads


def test_sse_payloads_keep_each_event():
    body = (
        "event: message\n"
        "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n"
        "\n"
        "data: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}\n"
        "\n"
        ": keep-alive\n"
        "\n"
    )
    assert sse_payloads(body) == [
        '{"jsonrpc":"2.0","method":"notifications/progress"}',
        '{"jsonrpc":"2.0","id":2,"result":{}}',
    ]


def test_bridge_tracks_the_session_and_protocol():
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-07-28"}},
                headers={"mcp-session-id": "sess-1"},
            )
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        sse = "data: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"ok\":true}}\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        bridge = Bridge("http://brush.test/api/mcp", "sekret", http)
        first = bridge.forward({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert json.loads(first[0])["result"]["protocolVersion"] == "2026-07-28"
        assert bridge.forward({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) == []
        lines = bridge.forward({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert json.loads(lines[0])["result"] == {"ok": True}

    assert seen[0].headers["x-api-token"] == "sekret"
    assert seen[0].headers["authorization"] == "Bearer sekret"
    assert seen[2].headers["mcp-session-id"] == "sess-1"
    assert seen[2].headers["mcp-protocol-version"] == "2026-07-28"


def test_unauthorized_becomes_a_jsonrpc_error():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        bridge = Bridge("http://brush.test/api/mcp", "nope", http)
        lines = bridge.forward({"jsonrpc": "2.0", "id": 7, "method": "initialize"})
    error = json.loads(lines[0])["error"]
    assert error["message"] == "unauthorized"
    assert error["code"] == -32000


def test_bridge_module_does_not_import_the_server():
    script = (
        "import sys\n"
        "import backend.app.agent.stdio_bridge\n"
        "assert 'backend.app.agent.mcp_server' not in sys.modules\n"
        "assert 'mcp' not in sys.modules\n"
    )
    subprocess.check_call([sys.executable, "-c", script])
