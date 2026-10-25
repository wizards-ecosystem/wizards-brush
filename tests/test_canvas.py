"""P20: canvas projects, the renderer, and their HTTP and MCP surfaces.

A document references gallery assets and never owns them; saving is a
compare-and-set; a missing source is a placeholder, never a crash; and the one
Pillow renderer gives the same pixels to every surface.
"""
from __future__ import annotations

import json
import uuid

import anyio
import pytest
from mcp import Client
from PIL import Image

from backend.app import canvas, db
from backend.app.agent import mcp_server
from backend.app.config import settings
from backend.app.models import JobStatus


def _asset(color=(200, 30, 30, 255), size=(4, 4), mode="RGBA") -> int:
    settings.ensure_dirs()
    path = settings.images_dir / f"canvas-{uuid.uuid4().hex[:8]}.png"
    Image.new(mode, size, color).save(path)
    return int(db.add_asset("image", path, width=size[0], height=size[1],
                            generator="import").id or 0)


def _doc(*layers, width=8, height=8, background="transparent"):
    return {"schema_version": 1, "width": width, "height": height, "color_space": "srgb",
            "background": background, "layers": list(layers)}


def _layer(lid, asset_id, **extra):
    return {"id": lid, "name": lid, "asset_id": asset_id, **extra}


def _create(client, document, name=None):
    response = client.post("/api/canvas", json={"name": name or f"poster {uuid.uuid4().hex[:6]}",
                                                "document": document})
    assert response.status_code == 200, response.text
    return response.json()


def _call(name: str, args: dict):
    async def run():
        async with Client(mcp_server.mcp) as mcp:
            return await mcp.call_tool(name, args)

    return anyio.run(run)


# ---- storage --------------------------------------------------------------------------------
def test_a_project_saves_revisions_and_keeps_every_one(client):
    red = _asset()
    made = _create(client, _doc(_layer("bg", red)))
    assert made["revision"] == made["head_revision"] == 1 and made["missing"] == []
    moved = _doc(_layer("bg", red, transform={"x": 2, "y": 1}))
    saved = client.put(f"/api/canvas/{made['id']}", json={"document": moved, "base_revision": 1})
    assert saved.status_code == 200 and saved.json()["revision"] == 2
    old = client.get(f"/api/canvas/{made['id']}?revision=1").json()
    assert old["document"]["layers"][0]["transform"]["x"] == 0
    # Saving the same document again is not a new revision.
    again = client.put(f"/api/canvas/{made['id']}", json={"document": moved, "base_revision": 2})
    assert again.json()["revision"] == 2


def test_a_save_from_an_old_revision_is_refused_not_applied(client):
    red = _asset()
    made = _create(client, _doc(_layer("bg", red)))
    client.put(f"/api/canvas/{made['id']}",
               json={"document": _doc(_layer("bg", red, opacity=0.5)), "base_revision": 1})
    stale = client.put(f"/api/canvas/{made['id']}",
                       json={"document": _doc(_layer("bg", red, visible=False)),
                             "base_revision": 1})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert stale.json()["state"]["current_revision"] == 2
    head = client.get(f"/api/canvas/{made['id']}").json()
    assert head["document"]["layers"][0]["opacity"] == 0.5


@pytest.mark.parametrize("document, fragment", [
    (_doc(width=0), "width"),
    (_doc(width=8192, height=8192), "pixels"),
    (_doc(background="red"), "background"),
    (_doc({"id": "Bad Id", "asset_id": 1}), "layer id"),
    (_doc({"id": "a", "asset_id": 1}, {"id": "a", "asset_id": 1}), "unique"),
    (_doc({"id": "a", "asset_id": 1, "blend_mode": "overlay"}), "blend_mode"),
    (_doc({"id": "a", "asset_id": 1, "transform": {"scale_x": 0}}), "scale_x"),
])
def test_a_bad_document_is_refused_with_a_reason(client, document, fragment):
    response = client.post("/api/canvas", json={"name": "x", "document": document})
    assert response.status_code == 400 and fragment in response.json()["detail"]


def test_new_references_must_be_live_and_masks_must_fit(client):
    red = _asset()
    missing = client.post("/api/canvas", json={"name": "x", "document": _doc(
        _layer("a", 999_999))})
    assert missing.status_code == 404
    wrong = client.post("/api/canvas", json={"name": "x", "document": _doc(
        _layer("a", red, mask_id=_asset(size=(2, 2))))})
    assert wrong.status_code == 400 and wrong.json()["code"] == "invalid_mask"


def test_a_trashed_source_is_a_placeholder_and_saves_still_work(client):
    red, blue = _asset(), _asset((0, 0, 255, 255))
    made = _create(client, _doc(_layer("a", red), _layer("b", blue)))
    db.delete_assets([blue])
    read = client.get(f"/api/canvas/{made['id']}").json()
    assert read["missing"] == [blue]
    assert [layer["id"] for layer in read["document"]["layers"]] == ["a", "b"]
    edited = _doc(_layer("a", red, opacity=0.5), _layer("b", blue))
    saved = client.put(f"/api/canvas/{made['id']}", json={"document": edited, "base_revision": 1})
    assert saved.status_code == 200 and saved.json()["missing"] == [blue]
    refused = client.post(f"/api/canvas/{made['id']}/flatten", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "missing_sources"
    partial = client.post(f"/api/canvas/{made['id']}/flatten", json={"allow_missing": True})
    assert partial.status_code == 200 and partial.json()["missing"] == [blue]


def test_deleting_a_project_keeps_its_assets(client):
    red = _asset()
    made = _create(client, _doc(_layer("a", red)))
    assert client.delete(f"/api/canvas/{made['id']}").json()["deleted_revisions"] == 1
    assert client.get(f"/api/canvas/{made['id']}").status_code == 404
    assert db.get_asset(red) is not None


def test_archiving_hides_a_project_from_the_list(client):
    made = _create(client, _doc())
    client.post(f"/api/canvas/{made['id']}/archive")
    assert made["id"] not in [p["id"] for p in client.get("/api/canvas").json()]
    assert made["id"] in [p["id"] for p in
                          client.get("/api/canvas?include_archived=true").json()]


def test_views_never_show_where_files_live(client):
    made = _create(client, _doc(_layer("a", _asset())))
    assert str(settings.images_dir) not in json.dumps(made)


# ---- the renderer ---------------------------------------------------------------------------
def _images(**by_id):
    return lambda asset_id: by_id.get(f"a{asset_id}")


def test_layers_composite_bottom_first_at_their_positions():
    document = canvas.Document.model_validate(_doc(
        _layer("bottom", 1, transform={"x": 0, "y": 0}),
        _layer("top", 2, transform={"x": 2, "y": 2})))
    image, missing = canvas.render(document, _images(
        a1=Image.new("RGBA", (4, 4), (255, 0, 0, 255)),
        a2=Image.new("RGBA", (4, 4), (0, 0, 255, 255))))
    assert missing == []
    assert image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert image.getpixel((3, 3)) == (0, 0, 255, 255)   # top wins where they overlap
    assert image.getpixel((5, 5)) == (0, 0, 255, 255)
    assert image.getpixel((7, 0)) == (0, 0, 0, 0)       # transparent background


def test_opacity_visibility_scale_and_masks():
    document = canvas.Document.model_validate(_doc(
        _layer("half", 1, opacity=0.5),
        _layer("hidden", 2, visible=False),
        _layer("big", 3, transform={"x": 4, "y": 4, "scale_x": 2, "scale_y": 2}),
        _layer("masked", 5, transform={"x": 4, "y": 0}, mask_id=4),
        background="#ffffff"))
    keep_left = Image.new("L", (4, 4), 0)
    for y in range(4):
        keep_left.putpixel((0, y), 255)
        keep_left.putpixel((1, y), 255)
    image, _ = canvas.render(document, _images(
        a1=Image.new("RGBA", (4, 4), (0, 0, 0, 255)),
        a2=Image.new("RGBA", (8, 8), (0, 255, 0, 255)),
        a3=Image.new("RGBA", (2, 2), (255, 0, 0, 255)),
        a4=keep_left,
        a5=Image.new("RGBA", (4, 4), (0, 0, 255, 255))))
    assert image.getpixel((1, 1)) in {(127, 127, 127, 255), (128, 128, 128, 255)}
    assert image.getpixel((7, 7)) == (255, 0, 0, 255)       # 2x2 scaled to fill 4..7
    assert image.getpixel((4, 4)) == (255, 0, 0, 255)
    assert image.getpixel((5, 1)) == (0, 0, 255, 255)       # the mask keeps the left half
    assert image.getpixel((7, 1)) == (255, 255, 255, 255)   # and hides the right half;
    assert image.getpixel((7, 0)) == (255, 255, 255, 255)   # the hidden layer drew nothing


def test_rotation_turns_about_the_layer_centre_clockwise():
    strip = Image.new("RGBA", (8, 2), (0, 0, 0, 0))
    for x in range(8):
        strip.putpixel((x, 0), (255, 0, 0, 255))
        strip.putpixel((x, 1), (255, 0, 0, 255))
    document = canvas.Document.model_validate(_doc(
        _layer("bar", 1, transform={"x": 0, "y": 3, "rotation": 90})))
    image, _ = canvas.render(document, _images(a1=strip))
    # A horizontal bar through the centre becomes a vertical one through it.
    assert image.getpixel((4, 0))[3] == 255 and image.getpixel((4, 7))[3] == 255
    assert image.getpixel((0, 4))[3] == 0 and image.getpixel((7, 4))[3] == 0


def test_multiply_and_screen_blend_over_the_base():
    document = canvas.Document.model_validate(_doc(
        _layer("base", 1), _layer("mul", 2, blend_mode="multiply"), width=4, height=4))
    images = _images(a1=Image.new("RGBA", (4, 4), (200, 100, 50, 255)),
                     a2=Image.new("RGBA", (4, 4), (128, 128, 128, 255)))
    image, _ = canvas.render(document, images)
    assert image.getpixel((0, 0))[:3] == (100, 50, 25)
    screen = canvas.Document.model_validate(_doc(
        _layer("base", 1), _layer("scr", 2, blend_mode="screen"), width=4, height=4))
    image, _ = canvas.render(screen, images)
    assert image.getpixel((0, 0))[:3] == (228, 178, 153)   # 255 - (255-a)(255-b)/255


def test_the_renderer_is_deterministic():
    document = canvas.Document.model_validate(_doc(
        _layer("a", 1, transform={"x": 1.5, "y": 0.5, "rotation": 33, "scale_x": 1.3})))
    images = _images(a1=Image.new("RGBA", (5, 3), (10, 200, 90, 255)))
    first, _ = canvas.render(document, images)
    second, _ = canvas.render(document, images)
    assert first.tobytes() == second.tobytes()


# ---- flatten and edits ----------------------------------------------------------------------
def test_flatten_writes_a_new_asset_with_lineage_and_leaves_the_document(client):
    red = _asset()
    made = _create(client, _doc(_layer("a", red, transform={"x": 2, "y": 2})))
    flat = client.post(f"/api/canvas/{made['id']}/flatten", json={})
    assert flat.status_code == 200, flat.text
    body = flat.json()
    asset = db.get_asset(body["asset_id"])
    assert asset is not None and asset.generator == "canvas"
    assert asset.meta["canvas"]["project_id"] == made["id"]
    assert asset.meta["canvas"]["revision"] == 1
    assert body["asset"]["id"] == body["asset_id"]
    with Image.open(asset.path) as image:
        assert image.size == (8, 8) and image.convert("RGBA").getpixel((3, 3)) == (200, 30, 30, 255)
    assert client.get(f"/api/canvas/{made['id']}").json()["head_revision"] == 1


def test_edit_inputs_name_the_layer_source_and_a_fitting_mask(client):
    red = _asset(size=(4, 4))
    made = _create(client, _doc(_layer("a", red)))
    mask = _asset((255, 255, 255, 255), size=(4, 4))
    response = client.post(f"/api/canvas/{made['id']}/edit-inputs",
                           json={"layer_id": "a", "mask_id": mask})
    assert response.status_code == 200
    assert response.json()["inputs"] == {"images": [red], "mask": mask}
    wrong = client.post(f"/api/canvas/{made['id']}/edit-inputs",
                        json={"layer_id": "a", "mask_id": _asset(size=(3, 3))})
    assert wrong.status_code == 400 and wrong.json()["code"] == "invalid_mask"
    composite = client.post(f"/api/canvas/{made['id']}/edit-inputs", json={})
    flattened = composite.json()["flattened"]
    assert composite.json()["inputs"]["images"] == [flattened["asset_id"]]


def test_a_finished_job_comes_back_as_a_layer_in_place(client):
    red = _asset()
    made = _create(client, _doc(_layer("a", red, transform={"x": 3, "y": 1, "rotation": 15})))
    output = _asset((0, 255, 0, 255))
    job = db.create_job("image_edit", {"prompt": "x"})
    db.mark_done(int(job.id or 0), {"asset_ids": [output]})
    assert db.get_job(int(job.id or 0)).status == JobStatus.done
    added = client.post(f"/api/canvas/{made['id']}/layers", json={
        "base_revision": 1, "job_id": job.id, "from_layer": "a"})
    assert added.status_code == 200, added.text
    layer = added.json()["document"]["layers"][-1]
    assert layer["asset_id"] == output and layer["transform"]["x"] == 3
    assert layer["transform"]["rotation"] == 15
    assert layer["lineage"] == {"job_id": job.id, "from_layer": "a"}
    pending = db.create_job("image_edit", {"prompt": "y"})
    not_yet = client.post(f"/api/canvas/{made['id']}/layers", json={
        "base_revision": 2, "job_id": pending.id})
    assert not_yet.status_code == 409


# ---- MCP ------------------------------------------------------------------------------------
def test_mcp_and_http_share_one_document_and_one_renderer(client):
    red = _asset()
    made = _call("create_canvas", {"name": f"mcp {uuid.uuid4().hex[:6]}",
                                   "document": _doc(_layer("a", red))}).structured_content
    assert made["client"] == "mcp" and made["revision"] == 1
    http = client.get(f"/api/canvas/{made['id']}").json()
    assert http["digest"] == made["digest"]
    blue = _asset((0, 0, 255, 255))
    added = _call("add_canvas_layer", {"canvas": made["id"], "base_revision": 1,
                                       "asset_id": blue}).structured_content
    assert added["revision"] == 2
    stale = _call("save_canvas", {"canvas": made["id"], "base_revision": 1,
                                  "document": _doc(_layer("a", red))})
    assert stale.is_error and "revision_conflict" in stale.content[0].text
    listed = _call("list_canvases", {}).structured_content["canvases"]
    assert made["id"] in [p["id"] for p in listed]
    flat = _call("flatten_canvas", {"canvas": made["id"]}).structured_content
    via_http = client.post(f"/api/canvas/{made['id']}/flatten", json={}).json()
    first, second = db.get_asset(flat["asset_id"]), db.get_asset(via_http["asset_id"])
    with Image.open(first.path) as a, Image.open(second.path) as b:
        assert a.convert("RGBA").tobytes() == b.convert("RGBA").tobytes()
    assert _call("get_canvas", {"canvas": made["id"], "revision": 1}).structured_content[
        "revision"] == 1
