"""MCP tools, through the Python SDK's in-process client.

The job runner is faked: a background task marks the job done with a tiny PNG
while the tool's wait loop is polling. Nothing here imports a model.
"""
from __future__ import annotations

import asyncio
import base64
import io
import itertools
import json

import anyio
from mcp import Client
from mcp.types import ImageContent
from PIL import Image

from backend.app import db
from backend.app.agent import mcp_server
from backend.app.routers import common, job_api
from backend.app.routers.common import persist_image

_TOOLS = {
    "list_kinds", "describe_kind", "generate", "edit", "get_job", "wait_job", "search_library",
}


def _error_body(result) -> dict:
    text = result.content[0].text
    return json.loads(text[text.find("{"):])


def _install_runner(monkeypatch):
    async def enqueue(kind, job_id, handler):
        async def run() -> None:
            db.mark_running(job_id)
            db.set_progress(job_id, 0.4, "sampling")
            await asyncio.sleep(0.05)
            asset_id = persist_image(
                Image.new("RGBA", (8, 8), (10, 20, 30, 255)),
                job_id=job_id, generator=kind, meta={"seed": 1}, tag="mcp",
            )
            db.mark_done(job_id, {"asset_ids": [asset_id]})

        asyncio.get_running_loop().create_task(run())

    monkeypatch.setattr(common, "enqueue", enqueue)
    monkeypatch.setattr(mcp_server, "WAIT_SLICE_SECONDS", 0.05)
    monkeypatch.setattr(job_api, "_POLL_SECONDS", 0.01)


def test_tool_list_and_schemas_come_from_the_registry(client):
    async def run():
        async with Client(mcp_server.mcp) as mcp:
            listed = await mcp.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == _TOOLS
            generate = next(tool for tool in listed.tools if tool.name == "generate")
            props = generate.input_schema["properties"]
            assert "prompt" in props
            assert "dry_run" in props and "inline_images" in props
            assert "ctx" not in props
            assert props["inline_images"].get("default") is False
            described = await mcp.call_tool("describe_kind", {"kind": "image_local"})
            schema = described.structured_content["params"]
            http = client.get("/api/jobs/kinds").json()
            expected = next(row["params"] for row in http if row["kind"] == "image_local")
            assert schema == expected

    anyio.run(run)


def test_generate_waits_and_reports_increasing_progress(client, monkeypatch):
    _install_runner(monkeypatch)
    seen: list[float] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append(progress)

    async def run():
        async with Client(mcp_server.mcp) as mcp:
            result = await mcp.call_tool(
                "generate",
                {"prompt": "a small cup", "params": {"seed": 11}, "inline_images": True},
                progress_callback=on_progress,
            )
            assert result.is_error is False
            body = result.structured_content
            assert body["settled"] is True
            assert body["job"]["client"] == "mcp"
            assert body["assets"]
            asset = body["assets"][0]
            assert asset["file_url"] == f"/api/assets/{asset['id']}/file"
            assert asset["client"] == "mcp"
            types = [block.type for block in result.content]
            assert "resource_link" in types
            assert any(isinstance(block, ImageContent) for block in result.content)
            stored = db.get_job(body["job_id"])
            assert stored is not None and stored.client == "mcp"

    anyio.run(run)
    assert seen
    assert all(later > earlier for earlier, later in itertools.pairwise(seen))


def test_dry_run_and_unknown_setting_share_the_http_shape(client):
    async def run():
        before = {job.id for job in db.list_jobs(limit=100_000)}
        async with Client(mcp_server.mcp) as mcp:
            dry = await mcp.call_tool("generate", {
                "prompt": "a cup on a table", "dry_run": True, "params": {"seed": 5},
            })
            assert dry.structured_content["dry_run"] is True
            assert dry.structured_content["would_queue"] == 1
            assert dry.structured_content["job_id"] is None
            bad = await mcp.call_tool("generate", {
                "prompt": "a cup", "params": {"promt": "nope"},
            })
            assert bad.is_error is True
            body = _error_body(bad)
            assert isinstance(body["detail"], str)
            assert body["code"] == "unknown_setting"
            assert body["field"] == "promt"
            assert "prompt" in body["valid_options"]
            assert body["suggestions"]
        after = {job.id for job in db.list_jobs(limit=100_000)}
        assert after == before

    anyio.run(run)


def test_edit_imports_a_data_url_and_refuses_http(client, monkeypatch):
    _install_runner(monkeypatch)
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    async def run():
        async with Client(mcp_server.mcp) as mcp:
            refused = await mcp.call_tool("edit", {
                "images": ["https://example.test/photo.png"],
                "instruction": "make it blue",
                "dry_run": True,
            })
            assert refused.is_error is True
            body = _error_body(refused)
            assert body["code"] == "invalid_request"
            assert body["field"] == "images"
            edited = await mcp.call_tool("edit", {
                "images": [data_url],
                "instruction": "make it blue",
                "dry_run": True,
                "params": {"seed": 9},
            })
            assert edited.is_error is False, edited
            assert edited.structured_content["dry_run"] is True
            assert edited.structured_content["params"]["prompt"] == "make it blue"

    anyio.run(run)


def test_resources_publish_kinds_jobs_and_assets(client, monkeypatch):
    _install_runner(monkeypatch)

    async def run():
        async with Client(mcp_server.mcp) as mcp:
            kinds = await mcp.read_resource("brush://kinds")
            catalogue = json.loads(kinds.contents[0].text)
            assert any(row["kind"] == "image_local" for row in catalogue["kinds"])
            one = await mcp.read_resource("brush://kinds/image_local")
            assert json.loads(one.contents[0].text)["kind"] == "image_local"
            try:
                missing = await mcp.read_resource("brush://kinds/no-such-kind")
            except Exception as exc:  # noqa: BLE001 — the SDK raises; the message is the contract
                assert "no-such-kind" in str(exc) or "unknown kind" in str(exc)
            else:
                assert getattr(missing, "is_error", False) or "unknown kind" in str(missing)

    anyio.run(run)


def test_streamable_http_initialize(client):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "brush-test", "version": "0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    direct = client.post("/api/mcp", json=payload, headers=headers)
    assert direct.status_code == 200, direct.text
    assert "protocolVersion" in direct.text
    slashed = client.post("/api/mcp/", json=payload, headers=headers)
    assert slashed.status_code == 200, slashed.text


def test_mcp_refuses_a_non_loopback_host_and_a_cross_site_fetch(client):
    assert client.post("/api/mcp", json={}, headers={"Host": "evil.example"}).status_code == 421
    cross = client.post("/api/mcp", json={}, headers={
        "Origin": "https://evil.example",
        "Sec-Fetch-Site": "cross-site",
    })
    assert cross.status_code == 403
