"""Tracking which parameters a handler actually read.

Two payoffs: metadata that describes what really happened, and visibility into
controls that are declared, rendered, and silently never read.
"""
from __future__ import annotations

from backend.app.params import ALWAYS_UNUSED, TrackedParams, split_metadata


def test_it_behaves_exactly_like_a_dict():
    """Subclassed rather than wrapped so no handler needs to change."""
    p = TrackedParams({"a": 1, "b": 2})
    assert p["a"] == 1
    assert p.get("b") == 2
    assert p.get("missing", "fallback") == "fallback"
    assert "a" in p
    assert sorted(p) == ["a", "b"]
    assert dict(**p) == {"a": 1, "b": 2}
    assert len(p) == 2


def test_reads_are_recorded_however_they_happen():
    p = TrackedParams({"a": 1, "b": 2, "c": 3, "d": 4})
    p["a"]
    p.get("b")
    "c" in p          # noqa: B015 — a membership test is how optional inputs are read
    p.pop("d")
    assert p.read_keys == {"a", "b", "c", "d"}


def test_a_missing_key_still_counts_as_read():
    """`params.get("mask_path")` returning None means the handler considered it."""
    p = TrackedParams({"a": 1})
    p.get("mask_path")
    assert "mask_path" in p.read_keys


def test_unused_lists_what_was_never_touched():
    p = TrackedParams({"prompt": "x", "guidance": 1.0, "phantom": 7})
    p.get("prompt")
    p.get("guidance")
    assert p.unused() == ["phantom"]


def test_routing_keys_are_never_reported():
    """Reporting bookkeeping keys would be noise that trains people to ignore it."""
    p = TrackedParams(dict.fromkeys(ALWAYS_UNUSED, "v"))
    assert p.unused() == []


def test_nothing_read_means_everything_is_unused():
    p = TrackedParams({"prompt": "x", "steps": 4})
    assert p.unused() == ["prompt", "steps"]


def test_unused_is_sorted_for_a_stable_report():
    p = TrackedParams({"zeta": 1, "alpha": 2, "mu": 3})
    assert p.unused() == ["alpha", "mu", "zeta"]


# ---- metadata -------------------------------------------------------------
def test_metadata_drops_parameters_that_had_no_effect():
    """Recording a setting that did nothing makes the metadata a lie: re-running
    from it produces something different and nothing explains why."""
    p = TrackedParams({"prompt": "castle", "steps": 8, "phantom_control": True})
    p.get("prompt")
    p.get("steps")
    kept, ignored = split_metadata(p)
    assert kept == {"prompt": "castle", "steps": 8}
    assert ignored == ["phantom_control"]


def test_metadata_is_unchanged_when_everything_was_read():
    p = TrackedParams({"prompt": "castle"})
    p.get("prompt")
    assert split_metadata(p) == ({"prompt": "castle"}, [])


def test_a_plain_dict_is_passed_through_untouched():
    """No read history means no evidence of disuse. Safe from any call site."""
    kept, ignored = split_metadata({"prompt": "x", "anything": 1})
    assert kept == {"prompt": "x", "anything": 1}
    assert ignored == []


def test_the_queue_hands_handlers_a_tracked_dict(client, no_queue):
    """End to end: what a handler receives is trackable, with no handler change."""
    import asyncio

    from backend.app import db, queue

    seen = {}

    def handler(job_id, params, cb):
        seen["type"] = type(params).__name__
        params.get("prompt")
        seen["unused_before_return"] = params.unused()
        return {"asset_ids": []}

    job = db.create_job("image_local", {"prompt": "p", "never_read": 1})
    lane = queue.JobQueue("test")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        lane._execute(job.id, handler, "image_local"))
    assert seen["type"] == "TrackedParams"
    assert seen["unused_before_return"] == ["never_read"]


def test_persist_image_reports_controls_the_handler_never_read(client, tmp_path):
    """The wiring, not just the mechanism.

    `image_meta` builds a NEW dict, so it carries no read history. Handing that
    to the tracker asks the wrong object what happened and the answer is always
    "everything was used" — which is how this feature spent its first life doing
    nothing at all. `persist_image` therefore takes the tracked params too.
    """
    from PIL import Image

    from backend.app import db
    from backend.app.params import TrackedParams
    from backend.app.routers.common import image_meta, persist_image

    params = TrackedParams({"negative_prompt": "", "steps": 4, "guidance": 1.0,
                            "quality": "Draft", "phantom_control": True})
    img = Image.new("RGB", (16, 16), "purple")
    meta = image_meta(params, img, prompt="a castle", seed=7)

    job = db.create_job("image_local", dict(params))
    asset_id = persist_image(img, job_id=job.id, generator="local_image:txt2img",
                             meta=meta, tag="test", params=params)

    saved = db.get_asset(asset_id).meta
    assert saved["ignored_params"] == ["phantom_control"]
    assert saved["prompt"] == "a castle"


def test_persist_image_stays_silent_without_tracked_params(client):
    """Omitting `params` must leave the metadata exactly as the handler built it.

    tools.py inherits the SOURCE image's metadata rather than describing its own
    request, so a report there would strip keys that were never its to strip.
    """
    from PIL import Image

    from backend.app import db
    from backend.app.models import PARAM_SCHEMA_VERSION
    from backend.app.routers.common import persist_image

    img = Image.new("RGB", (16, 16), "green")
    aid = persist_image(img, job_id=None, generator="upscale",
                        meta={"prompt": "inherited", "scale": 2}, tag="test")
    saved = db.get_asset(aid).meta
    assert "ignored_params" not in saved
    assert saved == {
        "prompt": "inherited", "scale": 2, "_v": PARAM_SCHEMA_VERSION,
    }


def test_persist_image_keeps_a_handler_derived_remote_adapter(client):
    """A fixed remote adapter is provenance, not an unused form control."""
    from PIL import Image

    from backend.app import db
    from backend.app.params import TrackedParams
    from backend.app.routers.common import persist_image

    params = TrackedParams({"loras": []})
    img = Image.new("RGB", (16, 16), "navy")
    job = db.create_job("image_colab", dict(params))
    asset_id = persist_image(
        img, job_id=job.id, generator="colab_a100",
        meta={"prompt": "p", "loras": [{"path": "local/private-adapter", "weight": 0.8}]},
        tag="test", params=params,
    )

    saved = db.get_asset(asset_id).meta
    assert saved["loras"] == [{"path": "local/private-adapter", "weight": 0.8}]
    assert "ignored_params" not in saved
