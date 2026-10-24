"""P03: versioned preset storage.

Old saved generators keep their exact behaviour; presets get revisions that
never change once written, curated revisions that cannot be edited in place,
and archive instead of delete, so every job's preset revision stays readable.
"""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from backend.app import client_tag, db
from backend.app.studio import catalogue, contracts

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "studio" / "presets.json").read_text())


def _custom(case: str = "no_lora", **changes):
    doc = copy.deepcopy(FIXTURES[case])
    doc.pop("id", None)
    doc.pop("revision", None)
    doc["name"] = changes.pop("name", f"{doc['name']} {uuid.uuid4().hex[:6]}")
    doc.update(changes)
    return doc


def _create(client, doc):
    response = client.post("/api/studio/presets", json=doc)
    assert response.status_code == 200, response.text
    return response.json()


# ---- saved generators are untouched -------------------------------------------------------
def test_presets_never_appear_as_saved_generators(client):
    preset = _create(client, _custom(name=f"Shared name {uuid.uuid4().hex[:6]}"))
    listed = client.get("/api/recipes").json()
    assert all(row["slug"] != preset["id"] for row in listed)
    assert client.get(f"/api/recipes/{preset['name']}").status_code == 404
    # A saved generator may share a preset's name: names are unique per family.
    saved = client.post("/api/recipes", json={"name": preset["name"], "kind": "image_local",
                                              "params": {"prompt": "x"}, "expose": ["prompt"]})
    assert saved.status_code == 200, saved.text


# ---- custom presets -----------------------------------------------------------------------
def test_create_read_and_revisions(client):
    created = _create(client, _custom())
    assert created["id"].startswith("custom/") and created["revision"] == 1
    assert created["origin"] == "custom" and created["client"] == "api"
    assert created["definition"]["model"]["resource"].startswith("hf:")
    edited = copy.deepcopy(created["definition"])
    edited["settings"]["quality"] = "High"
    updated = client.put(f"/api/studio/presets/{created['id']}",
                         json={"definition": edited, "base_revision": 1}).json()
    assert updated["revision"] == 2 and updated["definition"]["settings"]["quality"] == "High"
    old = client.get(f"/api/studio/presets/{created['id']}", params={"revision": 1}).json()
    assert old["definition"]["settings"]["quality"] == "Standard" and old["head_revision"] == 2
    revisions = client.get(f"/api/studio/presets/{created['id']}/revisions").json()
    assert [r["revision"] for r in revisions] == [1, 2] and revisions[1]["based_on"] == 1


def test_an_unchanged_save_writes_no_revision(client):
    created = _create(client, _custom())
    again = client.put(f"/api/studio/presets/{created['id']}",
                       json={"definition": created["definition"], "base_revision": 1}).json()
    assert again["revision"] == 1


def test_a_stale_save_conflicts_and_loses_neither_version(client):
    created = _create(client, _custom())
    first = copy.deepcopy(created["definition"])
    first["settings"]["aspect"] = "16:9"
    second = copy.deepcopy(created["definition"])
    second["settings"]["aspect"] = "9:16"
    url = f"/api/studio/presets/{created['id']}"
    assert client.put(url, json={"definition": first, "base_revision": 1}).json()["revision"] == 2

    stale = client.put(url, json={"definition": second, "base_revision": 1})
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "revision_conflict" and body["state"]["current_revision"] == 2
    assert client.get(url).json()["definition"]["settings"]["aspect"] == "16:9"

    kept = client.put(url, json={"definition": second, "base_revision": 1, "on_conflict": "copy"})
    assert kept.status_code == 200
    copy_view = kept.json()
    assert copy_view["id"] != created["id"]
    assert copy_view["cloned_from"] == {"id": created["id"], "revision": 1}
    assert copy_view["definition"]["settings"]["aspect"] == "9:16"
    assert client.get(url).json()["definition"]["settings"]["aspect"] == "16:9"


def test_unknown_fields_and_bad_values_name_their_path(client):
    bad = _custom()
    bad["adapters"] = [{"resource": "local:sha256:" + "a" * 64, "strength": 7}]
    response = client.post("/api/studio/presets", json=bad)
    assert response.status_code == 400
    assert response.json()["field"] == "definition.adapters.0.strength"
    extra = _custom()
    extra["surprise"] = 1
    assert client.post("/api/studio/presets", json=extra).json()["code"] == "unknown_field"


def test_the_curated_namespace_and_origin_are_reserved(client):
    assert client.post("/api/studio/presets",
                       json=_custom(id="curated/mine")).json()["code"] == "curated_reserved"
    assert client.post("/api/studio/presets",
                       json=_custom(origin="curated")).json()["code"] == "curated_reserved"


def test_an_ai_authored_preset_is_custom_with_its_author_recorded():
    with client_tag.use("mcp"):
        saved = catalogue.create(_custom(), client=client_tag.current())
    assert saved["origin"] == "custom" and saved["client"] == "mcp"


def test_archive_hides_without_forgetting(client):
    created = _create(client, _custom())
    url = f"/api/studio/presets/{created['id']}"
    edited = copy.deepcopy(created["definition"])
    edited["description"] = "second"
    client.put(url, json={"definition": edited, "base_revision": 1})
    assert client.post(f"{url}/archive").json()["archived"] is True
    assert all(p["id"] != created["id"] for p in client.get("/api/studio/presets").json())
    assert any(p["id"] == created["id"] for p in
               client.get("/api/studio/presets", params={"include_archived": True}).json())
    # A job that ran revision 1 can still read it.
    assert client.get(url, params={"revision": 1}).json()["definition"]["description"] == \
        created["definition"]["description"]
    assert client.put(url, json={"definition": edited, "base_revision": 2}).json()["code"] == \
        "preset_archived"
    assert client.post(f"{url}/unarchive").json()["archived"] is False


def test_clone_copies_a_specific_revision(client):
    created = _create(client, _custom("style_template"))
    url = f"/api/studio/presets/{created['id']}"
    edited = copy.deepcopy(created["definition"])
    edited["prompt"]["negative"] = "changed"
    client.put(url, json={"definition": edited, "base_revision": 1})
    clone = client.post(f"{url}/clone", json={"revision": 1, "name": "Storybook mine"}).json()
    assert clone["definition"]["prompt"]["negative"] == "photograph"
    assert clone["cloned_from"] == {"id": created["id"], "revision": 1}


# ---- curated manifests --------------------------------------------------------------------
def _ship(directory: Path, preset_doc: dict, lock: dict) -> None:
    preset = contracts.parse_preset(preset_doc)
    name = preset.id.split("/", 1)[1]
    (directory / f"{name}.r{preset.revision}.json").write_text(json.dumps(preset_doc))
    key, value = catalogue.lock_entry(preset)
    lock[key] = value
    (directory / "LOCK.json").write_text(json.dumps(lock))


def _curated(name: str, revision: int = 1, **changes) -> dict:
    doc = copy.deepcopy(FIXTURES["no_lora"])
    doc.update({
        "id": f"curated/{name}", "revision": revision, "origin": "curated",
        "name": f"Curated {name}",
        "curation": {
            "evidence": [{"kind": "evaluation", "ref": f"eval/{name}/r{revision}"}],
            "tested_targets": [{"target": "local", "hardware": "RTX 4070 Ti SUPER 16 GB"}],
            "limitations": ["long text is unreliable"],
        },
    })
    doc.update(changes)
    return doc


def test_curated_revisions_install_once_and_edits_become_copies(client, tmp_path):
    name = f"base-{uuid.uuid4().hex[:6]}"
    lock: dict = {}
    _ship(tmp_path, _curated(name), lock)
    assert catalogue.ensure_curated(tmp_path) == 1
    assert catalogue.ensure_curated(tmp_path) == 0          # idempotent
    url = f"/api/studio/presets/curated/{name}"
    shipped = client.get(url).json()
    assert shipped["origin"] == "curated" and shipped["client"] == "shipped"

    edit = copy.deepcopy(shipped["definition"])
    edit["settings"]["quality"] = "High"
    saved = client.put(url, json={"definition": edit, "base_revision": 1}).json()
    assert saved["origin"] == "custom" and saved["cloned_from"]["id"] == f"curated/{name}"
    assert "curation" not in saved["definition"] or not saved["definition"]["curation"]["evidence"]
    assert client.get(url).json()["definition"]["settings"]["quality"] == "Standard"

    _ship(tmp_path, _curated(name, revision=2, description="tuned"), lock)
    assert catalogue.ensure_curated(tmp_path) == 1
    assert client.get(url).json()["revision"] == 2
    assert client.get(url, params={"revision": 1}).json()["description"] == \
        FIXTURES["no_lora"]["description"]


def test_a_shipped_revision_edited_in_place_is_refused(tmp_path):
    name = f"tamper-{uuid.uuid4().hex[:6]}"
    lock: dict = {}
    _ship(tmp_path, _curated(name), lock)
    tampered = _curated(name, description="quietly retuned")
    (tmp_path / f"{name}.r1.json").write_text(json.dumps(tampered))
    assert catalogue.load_manifests(tmp_path) == {}
    assert catalogue.ensure_curated(tmp_path) == 0
    assert db.preset_head(f"curated/{name}") is None


def test_a_curated_preset_no_longer_shipped_is_archived_not_deleted(tmp_path):
    name = f"retired-{uuid.uuid4().hex[:6]}"
    lock: dict = {}
    _ship(tmp_path, _curated(name), lock)
    catalogue.ensure_curated(tmp_path)
    for path in tmp_path.glob(f"{name}.r*.json"):
        path.unlink()
    catalogue.ensure_curated(tmp_path)
    head = db.preset_head(f"curated/{name}")
    assert head is not None and head.archived_at is not None
    assert catalogue.get(f"curated/{name}", 1)["revision"] == 1


def test_the_shipped_catalogue_matches_its_lock():
    """Every file in studio/curated must load; none may be refused."""
    shipped = list(catalogue.CURATED_DIR.glob("*.r*.json"))
    loaded = catalogue.load_manifests()
    assert sum(len(v) for v in loaded.values()) == len(shipped)


# ---- the migration ------------------------------------------------------------------------
def test_the_upgrade_marks_every_old_recipe_as_a_saved_generator(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, text

    path = tmp_path / "gen.db"
    conn = sqlite3.connect(path)
    conn.executescript((Path(__file__).parent / "fixtures" / "legacy" /
                        "wizards-brush-0.1.8.sql").read_text())
    conn.close()
    engine = create_engine(f"sqlite:///{path}")
    db._migrate(engine=engine)
    with engine.begin() as handle:
        rows = handle.execute(text("SELECT slug, schema_version, preset_id, revision, origin "
                                   "FROM recipe ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [("ceramic-cup", 1, None, 1, ""),
                                            ("jacket-recolour", 1, None, 1, "")]
        # v1 names stay unique among saved generators.
        with pytest.raises(Exception, match="UNIQUE"):
            handle.execute(text(
                "INSERT INTO recipe (name, slug, description, kind, params_json, fields_json, "
                "inputs_json, defaulted_json, client, created_at, updated_at) VALUES "
                "('CERAMIC CUP', 'other', '', 'image_local', '{}', '[]', '{}', '[]', '', "
                "'2026-10-24', '2026-10-24')"))
