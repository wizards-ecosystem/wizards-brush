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
import uuid

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
    "list_recipes", "get_recipe", "save_recipe", "run_recipe",
    # P13: presets, plans, targets, resources, masks, cancellation, bytes.
    "list_targets", "list_processors", "list_presets", "get_preset", "preset_revisions",
    "list_resources", "get_resource", "rescan_resources", "prepare_resource_on_remote",
    "resource_preparation_status", "save_preset", "update_preset", "clone_preset",
    "archive_preset", "resolve_preset", "run_preset", "replay_job", "get_plan", "cancel_job",
    "import_mask", "fetch_asset",
    # P14: reference sets.
    "list_reference_sets", "get_reference_set", "save_reference_set", "update_reference_set",
    "archive_reference_set",
    # P15: Variant Sets.
    "preview_variant_set", "create_variant_set", "wait_variant_set", "cancel_variant_set",
    "retry_variant_set", "rerun_variant_item",
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


def _asset_ids() -> set[int]:
    rows, _total = db.search_assets(limit=100_000)
    return {int(row.id) for row in rows if row.id is not None}


def _png_data_url(color: str) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_edit_imports_a_data_url_and_refuses_http(client, monkeypatch):
    _install_runner(monkeypatch)
    data_url = _png_data_url("red")

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
            before = _asset_ids()
            edited = await mcp.call_tool("edit", {
                "images": [data_url],
                "instruction": "make it blue",
                "dry_run": True,
                "params": {"seed": 9},
            })
            assert edited.is_error is False, edited
            assert edited.structured_content["dry_run"] is True
            assert edited.structured_content["params"]["prompt"] == "make it blue"
            assert _asset_ids() == before

    anyio.run(run)


def test_edit_retry_does_not_import_the_data_url_again(client, monkeypatch):
    _install_runner(monkeypatch)
    request_id = "edit-retry-0000001"

    def imports() -> set[int]:
        rows, _total = db.search_assets(limit=100_000)
        return {
            int(row.id) for row in rows
            if row.id is not None and row.generator == "import"
        }

    async def run():
        async with Client(mcp_server.mcp) as mcp:
            before = imports()
            first = await mcp.call_tool("edit", {
                "images": [_png_data_url("red")],
                "instruction": "make it blue",
                "request_id": request_id,
                "wait": False,
            })
            assert first.is_error is False, first
            imported = imports() - before
            assert len(imported) == 1
            again = await mcp.call_tool("edit", {
                "images": [_png_data_url("green")],
                "instruction": "make it green",
                "request_id": request_id,
                "wait": False,
            })
            assert again.is_error is False, again
            assert again.structured_content["duplicate"] is True
            assert imports() - before == imported

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


def test_mcp_allows_a_non_loopback_host_when_the_token_matches(client, monkeypatch):
    from backend.app.config import settings

    monkeypatch.setattr(settings, "api_token", "tok123")
    headers = {"Host": "evil.example", "Authorization": "Bearer tok123"}
    allowed = client.post("/api/mcp", json={}, headers=headers)
    assert allowed.status_code not in {401, 421}


def test_mcp_rejects_an_oversized_body(client):
    from backend.app.main import app
    from backend.app.security import MAX_REQUEST_BYTES

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/mcp",
        "raw_path": b"/api/mcp",
        "query_string": b"",
        "headers": [
            (b"host", b"localhost"),
            (b"content-type", b"application/json"),
            (b"content-length", str(MAX_REQUEST_BYTES + 1).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def run():
        await app(scope, receive, send)

    anyio.run(run)
    assert sent[0]["status"] == 413
    assert b"request body too large" in sent[1]["body"]


def test_recipe_tools_publish_the_schema_and_keep_the_error_shape(client, monkeypatch):
    _install_runner(monkeypatch)
    name = f"MCP cup {uuid.uuid4().hex[:8]}"

    async def run():
        async with Client(mcp_server.mcp) as mcp:
            listed = await mcp.list_tools()
            run_tool = next(tool for tool in listed.tools if tool.name == "run_recipe")
            props = run_tool.input_schema["properties"]
            assert "inputs" in props and "dry_run" in props
            assert "ctx" not in props
            saved = await mcp.call_tool("save_recipe", {
                "name": name,
                "kind": "image_local",
                "params": {"prompt": "a cup", "seed": 11},
                "expose": ["prompt"],
                "field_descriptions": {"prompt": "What to draw."},
            })
            assert saved.is_error is False, saved
            body = saved.structured_content
            assert body["client"] == "mcp"
            assert body["input_schema"]["properties"]["prompt"]["description"] == "What to draw."
            catalogue = await mcp.call_tool("list_recipes", {})
            assert any(row["slug"] == body["slug"] for row in catalogue.structured_content["recipes"])
            detail = await mcp.call_tool("get_recipe", {"recipe": body["slug"]})
            assert detail.structured_content["params"]["seed"] == 11
            dry = await mcp.call_tool("run_recipe", {
                "recipe": name,
                "inputs": {"prompt": "a blue cup"},
                "dry_run": True,
                "wait": False,
            })
            assert dry.is_error is False, dry
            assert dry.structured_content["dry_run"] is True
            assert dry.structured_content["params"]["seed"] == 11
            assert dry.structured_content["recipe_slug"] == body["slug"]
            assert dry.structured_content["job_id"] is None
            refused = await mcp.call_tool("run_recipe", {
                "recipe": body["slug"],
                "inputs": {"steps": 4},
                "dry_run": True,
            })
            assert refused.is_error is True
            error = _error_body(refused)
            assert error["code"] == "unknown_setting"
            assert error["field"] == "steps"
            assert error["valid_options"] == ["prompt"]
            queued = await mcp.call_tool("run_recipe", {
                "recipe": body["slug"],
                "inputs": {"prompt": "a blue cup"},
                "wait": False,
            })
            assert queued.is_error is False, queued
            assert queued.structured_content["recipe_id"] == body["id"]
            job = db.get_job(queued.structured_content["job_id"])
            assert job is not None and job.client == "mcp"
            assert job.params["seed"] == 11
            document = await mcp.read_resource(f"brush://recipes/{body['slug']}")
            assert json.loads(document.contents[0].text)["slug"] == body["slug"]

    anyio.run(run)
