"""Build tests/fixtures/legacy/wizards-brush-0.1.8.sql: a pre-upgrade database.

Historical, run once: the dump is the schema that 0.1.8 shipped (migration head
0011) with representative rows, so every later migration is tested against
data shaped like a real install rather than against a database the new code
created for itself. Never regenerate it after a migration lands; that would
quietly turn the fixture into the new schema. The script refuses to.

    .venv/bin/python tests/fixtures/legacy/build_legacy_db.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_OUT = Path(__file__).with_name("wizards-brush-0.1.8.sql")
_HEAD = "0011_recipe_defaulted"


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="legacy-db-", dir=_ROOT / ".runtime" / "tmp"))
    os.environ["OUTPUT_DIR"] = str(scratch / "output")
    os.environ["LORA_DIR"] = str(scratch / "loras")
    sys.path.insert(0, str(_ROOT))
    from backend.app import db, migrations

    if migrations.MIGRATIONS[-1][0] != _HEAD:
        print(f"refusing: migration head is {migrations.MIGRATIONS[-1][0]}, not {_HEAD}; "
              "this fixture records the 0.1.8 schema and must not be rebuilt", file=sys.stderr)
        return 1
    db.init_db()
    path = scratch / "output" / "gen.db"
    conn = sqlite3.connect(path)
    _rows(conn)
    conn.commit()
    dump = "\n".join(line for line in conn.iterdump()) + "\n"
    conn.close()
    _OUT.write_text(
        "-- The Wizard's Brush 0.1.8 database (migration head 0011), representative rows.\n"
        "-- Generated once by build_legacy_db.py; never regenerate after a migration lands.\n"
        + dump, encoding="utf-8")
    print(f"wrote {_OUT.relative_to(_ROOT)}")
    return 0


def _params(**values: object) -> str:
    return json.dumps({"_v": 1, **values})


def _rows(conn: sqlite3.Connection) -> None:
    created = "2026-10-20 10:00:00.000000"
    finish_chain = [
        {"processor": "background_removal"},
        {"processor": "resize", "width": 512, "height": 512, "mode": "contain",
         "background": "transparent"},
    ]
    jobs = [
        # 1: a finished local txt2img with adapters, styles and ordered finishing.
        (1, "image_local", "done", _params(
            prompt="a fox in snow, soft watercolour washes", raw_prompt="a fox in snow",
            style_ids=["watercolor"], negative_prompt="", quality="Standard",
            loras=[{"path": "ink_wash.safetensors", "weight": 0.8}], sampler="default",
            steps=9, guidance=0.0, seed=1234, seed_mode="increment", batch=1,
            width=1024, height=1024, aspect="1:1", strength=0.6, finish="none",
            post_upscale=False, post_face=False, post_scale=4, post_detail=False,
            detail_prompt="", combinatorial=False, speed_mode=False,
            prompt_syntax="literal", model_variant="turbo", finish_steps=finish_chain),
         '{"asset_ids": [1]}', "fixture-request-000000000001", "Tongyi-MAI/Z-Image-Turbo",
         None, "ui"),
        # 2: a remote txt2img still queued when the old build stopped.
        (2, "image_colab", "queued", _params(
            prompt="a lighthouse at dusk", raw_prompt="a lighthouse at dusk", quality="High",
            steps=30, guidance=4.0, seed=99, batch=2, width=1328, height=1328, aspect="1:1",
            model_variant="quality", finish="none"),
         "{}", None, "Qwen/Qwen-Image-2512", None, "mcp"),
        # 3: a local inpaint with stored input paths (absolute, as real rows are).
        (3, "inpaint", "done", _params(
            prompt="a red scarf", raw_prompt="a red scarf", quality="Standard", steps=9,
            guidance=0.0, seed=7, batch=1, width=1024, height=1024, aspect="Match input",
            strength=0.6, inpaint_area="masked area", mask_grow=4, mask_padding=64,
            mask_blur=4, model_variant="turbo", image_path="/legacy/output/uploads/up_a.png",
            mask_path="/legacy/output/uploads/up_mask.png"),
         '{"asset_ids": [3]}', None, "Tongyi-MAI/Z-Image-Turbo", None, "ui"),
        # 4: a remote edit that failed.
        (4, "image_edit", "error", _params(
            prompt="make the jacket red", image_paths=["/legacy/output/images/a.png"],
            quality="Standard", steps=30, guidance=4.0, seed=5, input_fidelity="standard",
            background="opaque"),
         "{}", None, None, None, "api"),
        # 5: a tool job.
        (5, "upscale", "done", _params(scale=2, src_path="/legacy/output/images/a.png",
                                       source_asset_id=1),
         '{"asset_ids": [4]}', None, None, None, "ui"),
        # 6: a pre-schema row: no _v, no finish key, legacy flags only.
        (6, "image_local", "done", json.dumps({
            "prompt": "an old portrait", "steps": 9, "guidance": 1.0, "seed": 42,
            "post_upscale": True, "post_scale": 2, "model_variant": "turbo"}),
         '{"asset_ids": []}', None, None, None, ""),
        # 7: a Variant Set child.
        (7, "image_edit", "done", _params(
            prompt="the same character, standing", image_paths=["/legacy/output/images/a.png"],
            seed=11, variant={"set_id": 1, "item_id": 1, "key": "pose=standing"}),
         '{"asset_ids": [5]}', "vset-0f0f0f0f0f0f-item-1-attempt-1", None,
         "vset-00000000000000000000000000000001", "ui"),
    ]
    for (jid, kind, status, params, result, request_id, model_key, group_id, client) in jobs:
        conn.execute(
            "INSERT INTO job (id, kind, status, progress, message, params_json, result_json, "
            "error, priority, group_id, request_id, model_key, client, tip, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, kind, status, 1.0 if status == "done" else 0.0, "", params, result,
             "Traceback: boom" if status == "error" else "", 0, group_id, request_id,
             model_key, client, "", created))
    contents = [(1, "a" * 40), (2, "b" * 40), (3, "c" * 40), (4, "d" * 40), (5, "e" * 40)]
    for cid, digest in contents:
        conn.execute("INSERT INTO assetcontent (id, hash, size_bytes, mime_type, created_at) "
                     "VALUES (?,?,?,?,?)", (cid, digest, 1000 + cid, "image/png", created))
    assets: list[tuple[int, int | None, str, dict[str, object]]] = [
        (1, 1, "local_image:txt2img", {"prompt": "a fox in snow, soft watercolour washes",
                                         "raw_prompt": "a fox in snow", "seed": 1234,
                                         "model_variant": "turbo",
                                         "loras": [{"path": "ink_wash.safetensors",
                                                    "weight": 0.8}],
                                         "post": [{"processor": "background_removal"}]}),
        (2, None, "mask", {}),
        (3, 3, "local_image:inpaint", {"prompt": "a red scarf", "seed": 7}),
        (4, 5, "upscale", {"post": [{"operation": "upscale", "scale": 2}]}),
        (5, 7, "colab_edit", {"prompt": "the same character, standing",
                              "variant": {"set_id": 1, "item_id": 1}}),
    ]
    for aid, job_id, generator, meta in assets:
        conn.execute(
            "INSERT INTO asset (id, kind, path, filename, thumb, width, height, job_id, "
            "generator, meta_json, created_at, favorite, rating, grade_json, tags_json, "
            "caption, content_id, is_missing, needs_verify, enrich_level, used_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, "image", f"/legacy/output/images/{aid}.png", f"{aid}.png", None, 1024, 1024,
             job_id, generator, json.dumps({"_v": 1, **meta}), created, aid == 1, 0, "{}",
             "[]", "", aid, False, False, 0, 0))
    recipes: list[tuple[int, str, str, str, dict[str, object], list[dict[str, object]],
                        dict[str, object], list[str], str]] = [
        (1, "Ceramic cup", "ceramic-cup", "image_local",
         {"prompt": "a ceramic cup", "quality": "Standard", "guidance": 0.0, "seed": -1,
          "model_variant": "turbo"},
         [{"name": "prompt", "type": "textarea", "label": "Prompt", "description": "",
           "default": "a ceramic cup", "constraints": {}}],
         {"images": [], "mask": None, "last_frame": None}, ["guidance", "seed"], "ui"),
        (2, "Jacket recolour", "jacket-recolour", "image_edit",
         {"prompt": "make the jacket red", "quality": "Standard"},
         [{"name": "prompt", "type": "textarea", "label": "Prompt", "description": "",
           "default": "make the jacket red", "constraints": {}}],
         {"images": [1], "mask": None, "last_frame": None}, [], "mcp"),
    ]
    for rid, name, slug, kind, frozen, fields, inputs, defaulted, client in recipes:
        conn.execute(
            "INSERT INTO recipe (id, name, slug, description, kind, params_json, fields_json, "
            "inputs_json, defaulted_json, client, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, name, slug, "", kind, json.dumps(frozen), json.dumps(fields),
             json.dumps(inputs), json.dumps(defaulted), client, created, created))
    config = {"source_asset_ids": [1], "stages": [{
        "operation": "image_edit", "axes": [{"name": "pose", "values": ["standing", "sitting"]}],
        "prompt": "the same character, {{pose}}"}]}
    conn.execute("INSERT INTO variantrecipe (id, name, description, config_json, created_at, "
                 "updated_at) VALUES (1, 'Poses', '', ?, ?, ?)",
                 (json.dumps(config), created, created))
    conn.execute(
        "INSERT INTO variantset (id, name, recipe_id, recipe_json, operation, "
        "source_asset_ids_json, group_id, request_id, status, expected, counts_json, "
        "replay_json, created_at, updated_at) VALUES (1, 'Poses', 1, ?, 'image_edit', '[1]', "
        "'vset-00000000000000000000000000000001', NULL, 'active', 2, '{}', '{}', ?, ?)",
        (json.dumps(config), created, created))
    for iid, key, state, job_id in ((1, "pose=standing", "succeeded", 7),
                                    (2, "pose=sitting", "pending", None)):
        conn.execute(
            "INSERT INTO variantitem (id, set_id, stage, ordinal, key, values_json, "
            "source_asset_ids_json, params_json, job_id, attempts, history_json, asset_ids_json, "
            "state, state_reason, validation_state, validation_json, output_name, created_at, "
            "updated_at) VALUES (?,1,0,?,?,?,'[1]','{}',?,?,'[]',?,?,'','pending','[]',?,?,?)",
            (iid, iid - 1, key, json.dumps({"pose": key.split("=")[1]}), job_id,
             1 if job_id else 0, "[5]" if job_id else "[]", state, f"poses/{iid}", created,
             created))
    conn.execute("INSERT INTO prompthistory (id, kind, prompt, negative, params_json, favorite, "
                 "created_at) VALUES (1, 'image_local', 'a fox in snow', '', ?, 0, ?)",
                 (_params(prompt="a fox in snow"), created))
    conn.execute("INSERT INTO userpreset (id, type, name, payload_json, scope, created_at) "
                 "VALUES (1, 'style', 'Watercolour', '{\"text\": \"soft watercolour washes\"}', "
                 "'all', ?)", (created,))
    conn.execute("INSERT INTO collection (id, name, created_at) VALUES (1, 'Foxes', ?)",
                 (created,))
    conn.execute("INSERT INTO collectionitem (id, collection_id, asset_id, added_at) "
                 "VALUES (1, 1, 1, ?)", (created,))


if __name__ == "__main__":
    raise SystemExit(main())
