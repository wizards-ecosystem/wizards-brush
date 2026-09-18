"""Variant Sets end to end.

Children are driven through the real production path: the registered image-edit
handler, inline finishing, `image_meta` and `persist_image`. Only the network
call to the Remote GPU and the background-removal model are replaced. That makes
these tests about the behaviour the feature promises — a 48-variant set from one
reference asset that survives a restart, tracks every combination, retries only
what failed and leaves successful outputs alone — rather than about mocks.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest
from PIL import Image, ImageDraw

from backend.app import db, remote_gpu_client
from backend.app import queue as qmod
from backend.app.config import settings
from backend.app.generators import matting
from backend.app.models import JobStatus
from backend.app.routers import common
from backend.app.variant_sets import service, store

FAIL_REMOTE = "remote-fail"      # a prompt containing this makes the fake remote raise
MATTE_FAIL = (1, 2, 3)           # a remote image with this corner pixel breaks the matte


# ---- fixtures ---------------------------------------------------------------------------
@pytest.fixture()
def enqueued(monkeypatch):
    """Children are created as real queued jobs; lanes are driven by the test."""
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


def _image_asset(name: str, *, size=(96, 64), generator="colab_edit", meta=None):
    settings.ensure_dirs()
    img = Image.new("RGB", size, (200, 200, 190))
    ImageDraw.Draw(img).ellipse((24, 8, 72, 56), fill=(170, 40, 40))
    path = settings.images_dir / name
    img.save(path)
    asset = db.add_asset("image", path, width=size[0], height=size[1], generator=generator,
                         meta=meta or {"prompt": "source"})
    return asset


@pytest.fixture()
def source(client):
    return _image_asset("variant-source.png", generator="local_image:txt2img")


@pytest.fixture()
def remote(monkeypatch):
    """The Remote GPU, minus the network: returns an edited image or fails."""
    calls: list[dict] = []
    state: dict[str, set[str]] = {"matte_fail": set()}

    def run_remote(path, payload, progress_cb=None, *, job_id=None, **_kw):
        calls.append({"path": path, "payload": payload, "job_id": job_id})
        if FAIL_REMOTE in payload["prompt"]:
            raise RuntimeError("remote exploded")
        img = Image.new("RGB", (96, 64), (230, 230, 225))
        ImageDraw.Draw(img).ellipse((24, 8, 72, 56), fill=(40, 90, 160))
        if payload["prompt"] in state["matte_fail"]:
            img.putpixel((0, 0), MATTE_FAIL)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return {"image_b64": base64.b64encode(buf.getvalue()).decode()}

    monkeypatch.setattr(remote_gpu_client, "run_remote", run_remote)
    return {"calls": calls, **state}


@pytest.fixture()
def matte(monkeypatch):
    """Background removal without the model: a known elliptical matte."""
    def predict(img):
        if img.convert("RGB").getpixel((0, 0)) == MATTE_FAIL:
            raise RuntimeError("matting failed on this image")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).ellipse((24, 8, 72, 56), fill=255)
        return mask

    monkeypatch.setattr(matting, "unavailable_reason", lambda: None)
    monkeypatch.setattr(matting, "predict_matte", predict)


def run_child(job_id: int) -> None:
    """Execute one child job on a lane with its registered handler, then let the
    set react exactly as the queue listener would."""
    job = db.get_job(job_id)
    lane = qmod.JobQueue("test")
    asyncio.run(lane._execute(job_id, common.get_handler(job.kind), job.kind))
    asyncio.run(service.advance_after_job(job_id))


def _items(set_id):
    return store.items_for_set(set_id)


def recipe_48(source_id: int) -> dict:
    return {
        "sources": [source_id],
        "seed": {"mode": "per_variant", "value": 1234},
        "content": {"lighting": {
            "soft": "soft even neutral studio illumination",
            "dramatic": "directional high-contrast studio illumination",
        }},
        "stages": [{
            "operation": "image_edit",
            "axes": [
                {"name": "material", "values": ["wood", "steel", "glass"]},
                {"name": "finish", "values": ["matte", "gloss"]},
                {"name": "color", "values": ["red", "green", "blue", "white"]},
                {"name": "lighting", "values": ["soft", "dramatic"]},
            ],
            "prompt": "Render the object in {{finish}} {{color}} {{material}}, {{lighting}}",
            "params": {"quality": "Standard", "guidance": 4.0},
            "finishing": [
                {"processor": "background_removal"},
                {"processor": "resize", "width": 64, "height": 64, "mode": "contain",
                 "background": "transparent"},
            ],
            "validation": {"format": "PNG", "width": 64, "height": 64, "alpha": "required",
                           "corners_transparent": True},
            "naming": {"template": "{{material}}/{{color}}_{{finish}}_{{lighting}}"},
        }],
    }


# ---- the definition of done -----------------------------------------------------------------
def test_a_48_variant_reference_driven_edit_set(client, enqueued, source, remote, matte):
    recipe = recipe_48(source.id)
    body = {"name": "Object finishes", "request_id": "variant-set-dod-000001",
            "collection": {"mode": "new"}, "recipe": recipe}

    # (1) Preview 48 outputs before anything exists.
    preview = client.post("/api/variant-sets/preview", json={"recipe": recipe})
    assert preview.status_code == 200, preview.text
    shown = preview.json()
    assert shown["total"] == 48 and len(shown["items"]) == 48 and shown["collisions"] == []
    assert shown["items"][0]["output_name"] == "wood/red_matte_soft.png"
    assert shown["items"][0]["prompt"] == (
        "Render the object in matte red wood, soft even neutral studio illumination")
    assert db.list_jobs_by_group("vset-anything") == []

    # (2)(3) Persist the set and queue all 48 children — no 32/36 cap anywhere.
    r = client.post("/api/variant-sets", json=body)
    assert r.status_code == 200, r.text
    made = r.json()
    set_id = made["id"]
    assert made["created"] is True and made["expected"] == 48
    items = _items(set_id)
    assert len(items) == 48 and {i.state for i in items} == {"queued"}
    jobs = [db.get_job(i.job_id) for i in items]
    assert len(enqueued) == 48
    assert {j.kind for j in jobs} == {"image_edit"}          # an existing kind
    assert {j.group_id for j in jobs} == {made["group_id"]}  # the existing group column
    assert all(j.status == JobStatus.queued.value for j in jobs)

    # (4) One reference asset for every edit; (5) each with its own instruction.
    assert all(j.params["image_paths"] == [source.path] for j in jobs)
    assert len({j.params["prompt"] for j in jobs}) == 48
    assert all(j.params["batch"] == 1 for j in jobs)
    assert jobs[1].params["variant"]["key"] == \
        "material=wood,finish=matte,color=red,lighting=dramatic"
    assert jobs[1].params["variant"]["source_asset_ids"] == [source.id]
    # The item persists its effective params, identical to the job's.
    assert items[1].params["prompt"] == jobs[1].params["prompt"]

    # A retried submission resolves to the same set and queues nothing new.
    again = client.post("/api/variant-sets", json=body).json()
    assert again["id"] == set_id and again["created"] is False and len(enqueued) == 48

    # Make one generation fail and one lose its background removal.
    failing, unfinished = items[5], items[6]
    db.update_job(failing.job_id, params={**db.get_job(failing.job_id).params,
                                           "prompt": f"{FAIL_REMOTE} object"})
    remote["matte_fail"].add(db.get_job(unfinished.job_id).params["prompt"])

    for item in items:
        run_child(int(item.job_id))

    # (7) Every combination tracked independently.
    counts = client.get(f"/api/variant-sets/{set_id}").json()["counts"]
    assert counts["succeeded"] == 46 and counts["failed"] == 1 and counts["invalid"] == 1
    got = {i.id: i for i in _items(set_id)}
    assert "remote exploded" in got[failing.id].state_reason
    assert got[unfinished.id].state == "invalid"
    finishing = next(r for r in got[unfinished.id].validation if r["validator"] == "finishing")
    assert finishing["status"] == "fail" and finishing["details"]["missing"] == ["background_removal"]

    # (10)(11) Finishing ran inline and the final files validate.
    done = got[items[0].id]
    assert done.state == "succeeded" and done.validation_state == "passed"
    asset = db.get_asset(done.asset_ids[0])
    with Image.open(asset.path) as out:
        assert out.format == "PNG" and out.mode == "RGBA" and out.size == (64, 64)
        assert out.getpixel((0, 0))[3] == 0
    assert [p["operation"] for p in asset.meta["post"]] == ["background_removal", "resize"]
    assert asset.meta["post"][0]["model"] == matting.MODEL_ID
    assert asset.meta["variant"]["set_id"] == set_id
    assert asset.meta["variant"]["values"]["lighting"] == "soft"
    assert asset.meta["variant"]["source_asset_ids"] == [source.id]

    # (8)(9) Retry only the failed and invalid; successful children untouched.
    before = {i.id: (i.job_id, i.attempts, i.asset_ids) for i in _items(set_id)}
    db.update_job(failing.job_id, params={**db.get_job(failing.job_id).params,
                                           "prompt": "a working object"})
    remote["matte_fail"].clear()
    retried = client.post(f"/api/variant-sets/{set_id}/retry", json={}).json()
    assert retried == {"retried": 2, "submitted": 2}
    after = {i.id: i for i in _items(set_id)}
    for item_id, (job_id, attempts, assets) in before.items():
        if item_id in (failing.id, unfinished.id):
            assert after[item_id].attempts == 2 and after[item_id].job_id != job_id
            assert after[item_id].history[-1]["job_id"] == job_id
        else:
            assert (after[item_id].job_id, after[item_id].attempts,
                    after[item_id].asset_ids) == (job_id, attempts, assets)
    # A retry reproduces what failed: the stored effective params, attempt 2.
    retry_job = db.get_job(after[unfinished.id].job_id)
    assert retry_job.params["prompt"] == db.get_job(unfinished.job_id).params["prompt"]
    assert retry_job.request_id.endswith("attempt-2")

    for item_id in (failing.id, unfinished.id):
        run_child(int(after[item_id].job_id))
    final = client.get(f"/api/variant-sets/{set_id}").json()
    assert final["status"] == "complete" and final["counts"]["succeeded"] == 48

    # (12)(14) Deterministic names, and the set lands in its own collection.
    names = [i.output_name for i in _items(set_id)]
    assert len(set(names)) == 48 and names[0] == "wood/red_matte_soft.png"
    assert len(db.collection_asset_ids(final["collection_id"])) == 48


# ---- staged derivation ---------------------------------------------------------------------------
def _staged(source_id):
    return {
        "sources": [source_id],
        "seed": {"mode": "fixed", "value": 7},
        "stages": [
            {"name": "base", "operation": "image_edit",
             "axes": [{"name": "material", "values": ["wood", "steel"]}],
             "prompt": "Make it {{material}}"},
            {"name": "views", "operation": "image_edit",
             "axes": [{"name": "angle", "values": ["front", "side"]}],
             "prompt": "Show the {{material}} object from the {{angle}}"},
        ],
    }


def test_staged_variants_derive_from_each_successful_parent(client, enqueued, source, remote):
    made = client.post("/api/variant-sets", json={"recipe": _staged(source.id)}).json()
    set_id = made["id"]
    items = _items(set_id)
    stage0 = [i for i in items if i.stage == 0]
    stage1 = [i for i in items if i.stage == 1]
    assert len(stage0) == 2 and len(stage1) == 4
    # Only the first stage has jobs; the rest wait without inventing a job status.
    assert all(i.state == "queued" for i in stage0)
    assert all(i.state == "pending" and i.job_id is None for i in stage1)
    assert [i.key for i in stage1] == [
        "material=wood,angle=front", "material=wood,angle=side",
        "material=steel,angle=front", "material=steel,angle=side"]

    wood, steel = stage0
    db.update_job(steel.job_id, params={**db.get_job(steel.job_id).params,
                                         "prompt": f"{FAIL_REMOTE} steel"})
    run_child(int(wood.job_id))
    run_child(int(steel.job_id))

    got = {i.id: i for i in _items(set_id)}
    wood_output = got[wood.id].asset_ids[0]
    wood_children = [got[i.id] for i in stage1 if i.parent_item_id == wood.id]
    steel_children = [got[i.id] for i in stage1 if i.parent_item_id == steel.id]
    # Stage 2 runs on stage 1's output, and says so in its lineage.
    for child in wood_children:
        assert child.state == "queued"
        job = db.get_job(child.job_id)
        assert job.params["image_paths"] == [db.get_asset(wood_output).path]
        assert job.params["variant"]["source_asset_ids"] == [wood_output]
        assert job.params["prompt"].startswith("Show the wood object")
    # A failed parent leaves its dependents clearly blocked, with no job.
    for child in steel_children:
        assert child.state == "blocked" and child.job_id is None
        assert "failed" in child.state_reason

    for child in wood_children:
        run_child(int(child.job_id))
    status = client.get(f"/api/variant-sets/{set_id}").json()
    assert status["status"] == "incomplete"
    assert status["counts"]["blocked"] == 2 and status["counts"]["succeeded"] == 3

    # Retrying the parent unblocks its dependents; they run once it succeeds.
    db.update_job(steel.job_id, params={**db.get_job(steel.job_id).params, "prompt": "steel"})
    assert client.post(f"/api/variant-sets/{set_id}/retry").json()["retried"] == 1
    got = {i.id: i for i in _items(set_id)}
    assert all(got[c.id].state == "pending" for c in steel_children)
    run_child(int(got[steel.id].job_id))
    got = {i.id: i for i in _items(set_id)}
    for child in steel_children:
        assert got[child.id].state == "queued"
        run_child(int(got[child.id].job_id))
    final = client.get(f"/api/variant-sets/{set_id}").json()
    assert final["status"] == "complete" and final["counts"]["succeeded"] == 6


def test_rerun_one_variant_on_purpose_and_cascade(client, enqueued, source, remote):
    set_id = client.post("/api/variant-sets", json={"recipe": _staged(source.id)}).json()["id"]
    for item in [i for i in _items(set_id) if i.stage == 0]:
        run_child(int(item.job_id))
    for item in [i for i in _items(set_id) if i.stage == 1]:
        run_child(int(item.job_id))
    wood = _items(set_id)[0]
    children = [i for i in _items(set_id) if i.parent_item_id == wood.id]
    first_seed = wood.params["seed"]

    # Without cascade the dependents keep what they made from the old output.
    r = client.post(f"/api/variant-sets/{set_id}/items/{wood.id}/rerun", json={"reseed": True})
    assert r.status_code == 200, r.text
    rerun = store.get_item(wood.id)
    assert rerun.state == "queued" and rerun.attempts == 2
    assert rerun.params["seed"] != first_seed
    assert rerun.history[-1]["seed"] == first_seed and rerun.history[-1]["state"] == "succeeded"
    assert all(store.get_item(c.id).state == "succeeded" for c in children)
    run_child(int(rerun.job_id))

    # With cascade they rebuild from the new output.
    client.post(f"/api/variant-sets/{set_id}/items/{wood.id}/rerun", json={"cascade": True})
    assert all(store.get_item(c.id).state == "pending" for c in children)
    run_child(int(store.get_item(wood.id).job_id))
    new_output = store.get_item(wood.id).asset_ids[0]
    for child in children:
        job = db.get_job(store.get_item(child.id).job_id)
        assert job.params["variant"]["source_asset_ids"] == [new_output]

    # In-flight and blocked variants cannot be rerun.
    busy = client.post(f"/api/variant-sets/{set_id}/items/{children[0].id}/rerun")
    assert busy.status_code == 409


def test_cancel_remaining_keeps_finished_results(client, enqueued, source, remote):
    recipe = {"sources": [source.id], "stages": [{
        "operation": "image_edit", "prompt": "{{a}} {{b}}",
        "axes": [{"name": "a", "values": ["x", "y"]}, {"name": "b", "values": ["1", "2"]}]}]}
    set_id = client.post("/api/variant-sets", json={"recipe": recipe}).json()["id"]
    first = _items(set_id)[0]
    run_child(int(first.job_id))
    assert client.post(f"/api/variant-sets/{set_id}/cancel").status_code == 200
    view = client.get(f"/api/variant-sets/{set_id}").json()   # reading reconciles
    states = {i["id"]: i["state"] for i in view["items"]}
    assert states[first.id] == "succeeded"
    assert sorted(states.values()) == ["canceled", "canceled", "canceled", "succeeded"]
    assert view["status"] == "canceled"
    assert all(db.get_job(i.job_id).status == "canceled"
               for i in _items(set_id) if i.id != first.id)

    # Canceled work is resumed only when asked for.
    assert client.post(f"/api/variant-sets/{set_id}/retry").json()["retried"] == 0
    resumed = client.post(f"/api/variant-sets/{set_id}/retry",
                          json={"include_canceled": True}).json()
    assert resumed == {"retried": 3, "submitted": 3}
    assert store.get_item(first.id).attempts == 1


# ---- restart ----------------------------------------------------------------------------------------
def test_a_restart_settles_what_it_interrupted_and_resumes_the_rest(client, enqueued, source,
                                                                   remote):
    set_id = client.post("/api/variant-sets", json={"recipe": _staged(source.id)}).json()["id"]
    wood, steel = [i for i in _items(set_id) if i.stage == 0]

    # The old process: steel was mid-run; wood finished but nobody processed it.
    db.update_job(steel.job_id, status=JobStatus.running.value)
    job = db.get_job(wood.job_id)
    asyncio.run(qmod.JobQueue("test")._execute(int(wood.job_id), common.get_handler(job.kind),
                                               job.kind))
    assert store.get_item(wood.id).state == "queued"

    # The new process, in the order main.lifespan runs it.
    assert db.reconcile_orphans() >= 1
    lane = qmod.LANES[qmod.lane_for("image_edit")]
    try:
        asyncio.run(qmod.resume_queued())
        asyncio.run(service.reconcile_all())
        got = {i.id: i for i in _items(set_id)}
        assert got[steel.id].state == "canceled"
        assert got[steel.id].state_reason == "interrupted by restart"
        assert got[wood.id].state == "succeeded"
        wood_children = [i for i in got.values() if i.parent_item_id == wood.id]
        steel_children = [i for i in got.values() if i.parent_item_id == steel.id]
        assert all(c.state == "queued" for c in wood_children)
        assert all(c.state == "blocked" for c in steel_children)
        # Reconciling twice changes nothing and never duplicates a job.
        jobs_before = len(db.list_jobs_by_group(store.get_set(set_id).group_id))
        asyncio.run(service.reconcile_all())
        assert len(db.list_jobs_by_group(store.get_set(set_id).group_id)) == jobs_before
    finally:
        lane._pending.clear()


def test_a_submission_interrupted_before_it_was_recorded_is_recovered(client, enqueued,
                                                                     source):
    """The job exists (its request id is deterministic) but the item never
    heard: reconciliation must adopt that job, not create a second one."""
    recipe = {"sources": [source.id], "stages": [{"operation": "image_edit", "prompt": "p {{a}}",
                                                  "axes": [{"name": "a", "values": ["x"]}]}]}
    set_id = client.post("/api/variant-sets", json={"recipe": recipe}).json()["id"]
    item = _items(set_id)[0]
    store.transition(item.id, state="pending", job_id=None, attempts=0)
    asyncio.run(service.reconcile_set(set_id))
    again = store.get_item(item.id)
    assert again.job_id == item.job_id and again.attempts == 1 and again.state == "queued"
    assert len(db.list_jobs_by_group(store.get_set(set_id).group_id)) == 1


def test_queue_page_retry_of_a_failed_child_retries_its_variant(client, enqueued, source, remote):
    recipe = {"sources": [source.id], "stages": [{"operation": "image_edit",
                                                  "prompt": f"{FAIL_REMOTE} {{{{a}}}}",
                                                  "axes": [{"name": "a", "values": ["x"]}]}]}
    set_id = client.post("/api/variant-sets", json={"recipe": recipe}).json()["id"]
    item = _items(set_id)[0]
    run_child(int(item.job_id))
    assert store.get_item(item.id).state == "failed"
    r = client.post(f"/api/jobs/{item.job_id}/rerun?reseed=false")
    assert r.status_code == 200, r.text
    assert r.json()["variant_set_id"] == set_id and r.json()["replaced_job_id"] == item.job_id
    again = store.get_item(item.id)
    assert again.attempts == 2 and again.job_id == r.json()["job_id"]


# ---- masks ----------------------------------------------------------------------------------------------
def test_masks_are_durable_assets_passed_to_the_stage(client, enqueued, source):
    mask = Image.new("L", (96, 64), 0)
    ImageDraw.Draw(mask).rectangle((20, 10, 60, 50), fill=255)
    buf = io.BytesIO()
    mask.save(buf, "PNG")
    up = client.post("/api/variant-sets/masks", files={"mask": ("m.png", buf.getvalue(), "image/png")})
    assert up.status_code == 200, up.text
    mask_id = up.json()["asset_id"]
    mask_asset = db.get_asset(mask_id)
    assert mask_asset.generator == "mask"

    recipe = {"sources": [source.id], "masks": {"region": {"asset_id": mask_id}},
              "stages": [{"operation": "inpaint", "mask": "region", "prompt": "a {{m}} panel",
                          "axes": [{"name": "m", "values": ["cork", "felt"]}],
                          "params": {"strength": 0.8}}]}
    r = client.post("/api/variant-sets", json={"recipe": recipe})
    assert r.status_code == 200, r.text
    jobs = [db.get_job(i.job_id) for i in _items(r.json()["id"])]
    assert {j.kind for j in jobs} == {"inpaint"}
    assert all(j.params["mask_path"] == mask_asset.path for j in jobs)
    assert all(j.params["image_path"] == source.path for j in jobs)
    assert all(j.params["strength"] == 0.8 for j in jobs)

    empty = io.BytesIO()
    Image.new("L", (8, 8), 0).save(empty, "PNG")
    assert client.post("/api/variant-sets/masks",
                       files={"mask": ("e.png", empty.getvalue(), "image/png")}).status_code == 400


def test_set_listing_and_item_detail(client, enqueued, source):
    recipe = {"sources": [source.id], "stages": [{"operation": "image_edit", "prompt": "p {{a}}",
                                                  "axes": [{"name": "a", "values": ["x", "y"]}]}]}
    made = client.post("/api/variant-sets", json={"recipe": recipe, "name": "Listing"}).json()
    listed = client.get("/api/variant-sets").json()
    assert any(s["id"] == made["id"] and s["name"] == "Listing" for s in listed)
    item = _items(made["id"])[0]
    detail = client.get(f"/api/variant-sets/{made['id']}/items/{item.id}").json()
    assert detail["key"] == "a=x" and detail["params"]["prompt"] == "p x"
    assert detail["job_status"] == "queued"
    only_y = client.get(f"/api/variant-sets/{made['id']}/items", params={"state": "queued"}).json()
    assert len(only_y) == 2
    assert client.get("/api/variant-sets/999999").status_code == 404
    # An active set cannot be deleted; a finished one can, and its jobs stay.
    assert client.delete(f"/api/variant-sets/{made['id']}").status_code == 409
    client.post(f"/api/variant-sets/{made['id']}/cancel")
    client.get(f"/api/variant-sets/{made['id']}")
    assert client.delete(f"/api/variant-sets/{made['id']}").status_code == 200
    assert db.get_job(item.job_id) is not None


def test_the_listener_ignores_ordinary_jobs(client):
    job = db.create_job("image_local", {"prompt": "ordinary"})
    assert service.on_job_terminal(db.get_job(job.id)) is None
    assert json.loads(json.dumps(service.is_variant_group("vset-abc")))


# ---- export -------------------------------------------------------------------------------------------
def _two_by_two(source_id, **stage):
    return {"sources": [source_id], "seed": {"mode": "fixed", "value": 11}, "stages": [{
        "operation": "image_edit", "prompt": "Make it {{tone}} and {{size}}",
        "axes": [{"name": "tone", "values": ["warm", "cool"]},
                 {"name": "size", "values": ["small", "large"]}],
        "naming": {"template": "{{tone}}-{{size}}", "prefix": "set-"}, **stage}]}


def _zip(response):
    import zipfile

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return archive, json.loads(archive.read("wizards-brush-manifest.json"))


def test_export_is_the_successful_set_under_deterministic_names(client, enqueued, source, remote):
    set_id = client.post("/api/variant-sets", json={
        "name": "Tone study", "recipe": _two_by_two(source.id)}).json()["id"]
    items = _items(set_id)
    db.update_job(items[3].job_id, params={**db.get_job(items[3].job_id).params,
                                            "prompt": f"{FAIL_REMOTE} x"})
    for item in items:
        run_child(int(item.job_id))

    r = client.post(f"/api/variant-sets/{set_id}/export", json={})
    assert r.status_code == 200, r.text
    assert f"tone-study-{set_id}.zip" in r.headers["content-disposition"]
    archive, manifest = _zip(r)
    assert sorted(n for n in archive.namelist() if n.endswith(".png")) == [
        "set-cool-small.png", "set-warm-large.png", "set-warm-small.png"]

    by_name = {a["archive_path"]: a for a in manifest["assets"]}
    record = by_name["set-warm-large.png"]
    variant = record["variant"]
    assert variant["key"] == "tone=warm,size=large"
    assert variant["values"] == {"tone": "warm", "size": "large"}
    assert variant["output_name"] == "set-warm-large.png"
    assert variant["source_asset_ids"] == [source.id] and variant["parent"] is None
    assert variant["operation"] == "image_edit"
    assert variant["prompt"] == "Make it warm and large" and variant["seed"] == 11
    assert variant["model"] == settings.qwen_edit_model
    assert variant["validation"]["state"] == "passed"
    assert record["asset_id"] == store.get_item(items[1].id).asset_ids[0]

    summary = manifest["variant_set"]
    assert summary["exported"] == 3 and summary["expected"] == 4
    assert summary["missing"] == [{"key": "tone=cool,size=large", "stage": 0, "state": "failed",
                                   "reason": "remote exploded"}]
    assert summary["recipe"]["stages"][0]["prompt"] == "Make it {{tone}} and {{size}}"
    assert summary["replay"]["base_seed"] == 11
    # Server paths never leave in a manifest; lineage is by asset id.
    text = json.dumps(manifest)
    assert str(settings.output_path) not in text and "image_paths" not in text

    # The manifest endpoint describes the same export without building it.
    preview = client.get(f"/api/variant-sets/{set_id}/manifest").json()
    assert [a["archive_path"] for a in preview["assets"]] == \
        [a["archive_path"] for a in manifest["assets"]]


def test_export_honours_the_generation_metadata_privacy_setting(client, enqueued, source, remote):
    set_id = client.post("/api/variant-sets", json={"recipe": _two_by_two(source.id)}).json()["id"]
    for item in _items(set_id):
        run_child(int(item.job_id))
    previous = settings.load_overrides()
    settings.save_overrides({**previous, "embed_metadata": False})
    try:
        archive, manifest = _zip(client.post(f"/api/variant-sets/{set_id}/export"))
    finally:
        settings.save_overrides(previous)
    assert len([n for n in archive.namelist() if n.endswith(".png")]) == 4
    variant = manifest["assets"][0]["variant"]
    for secret in ("prompt", "negative_prompt", "seed", "model", "params"):
        assert secret not in variant
    assert manifest["assets"][0]["generation"] is None
    recipe = manifest["variant_set"]["recipe"]
    assert "prompt" not in recipe["stages"][0] and "content" not in recipe
    assert recipe["stages"][0]["axes"][0]["name"] == "tone"
    assert "base_seed" not in manifest["variant_set"]["replay"]
    assert "Make it" not in json.dumps(manifest)


def test_export_of_a_staged_set_and_of_trashed_outputs(client, enqueued, source, remote):
    set_id = client.post("/api/variant-sets", json={"recipe": _staged(source.id)}).json()["id"]
    for stage in (0, 1):
        for item in [i for i in _items(set_id) if i.stage == stage]:
            run_child(int(item.job_id))
    _archive, final_only = _zip(client.post(f"/api/variant-sets/{set_id}/export"))
    assert sorted(a["archive_path"] for a in final_only["assets"]) == [
        "steel_front.png", "steel_side.png", "wood_front.png", "wood_side.png"]
    child = next(a for a in final_only["assets"] if a["archive_path"] == "wood_front.png")
    parent = _items(set_id)[0]
    assert child["variant"]["parent"] == {"item_id": parent.id, "key": "material=wood",
                                          "asset_id": parent.asset_ids[0]}
    assert child["variant"]["source_asset_ids"] == [parent.asset_ids[0]]

    _archive, everything = _zip(client.post(f"/api/variant-sets/{set_id}/export",
                                            json={"include_intermediate": True}))
    assert {"stage-1/wood.png", "stage-1/steel.png"} <= {
        a["archive_path"] for a in everything["assets"]}

    # A trashed output is not exported and is accounted for.
    trashed = next(i for i in _items(set_id) if i.stage == 1)
    db.delete_asset(trashed.asset_ids[0])
    _archive, after = _zip(client.post(f"/api/variant-sets/{set_id}/export"))
    assert len(after["assets"]) == 3
    assert after["variant_set"]["missing"][0]["key"] == trashed.key
    assert "trash" in after["variant_set"]["missing"][0]["reason"]


def test_a_deleted_set_never_lends_its_ids_or_jobs_to_the_next(client, enqueued, source):
    """Regression: SQLite recycles the highest freed rowid. A set deleted while
    its jobs and assets survive must not have its ids — and so its deterministic
    child request ids and provenance — handed to the next set."""
    recipe = {"sources": [source.id], "stages": [{"operation": "image_edit", "prompt": "p {{a}}",
                                                  "axes": [{"name": "a", "values": ["x", "y"]}]}]}
    first = client.post("/api/variant-sets", json={"recipe": recipe}).json()
    old_items = _items(first["id"])
    client.post(f"/api/variant-sets/{first['id']}/cancel")
    client.get(f"/api/variant-sets/{first['id']}")
    assert client.delete(f"/api/variant-sets/{first['id']}").status_code == 200

    second = client.post("/api/variant-sets", json={"recipe": recipe}).json()
    assert second["id"] != first["id"]
    new_items = _items(second["id"])
    assert not {i.id for i in new_items} & {i.id for i in old_items}
    new_jobs = [db.get_job(i.job_id) for i in new_items]
    assert all(i.state == "queued" for i in new_items) and len(enqueued) == 4
    assert {j.group_id for j in new_jobs} == {second["group_id"]}
    assert all(j.params["variant"]["group_id"] == second["group_id"] for j in new_jobs)
    assert all(second["group_id"] in j.request_id for j in new_jobs)
