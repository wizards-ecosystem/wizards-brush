"""Saved generation recipes: the compiler, the schema, and migration 0010.

Running a recipe is covered in test_recipes_api.py. This file stays on the
pure compiler and on the table the migration creates.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.app.config import settings
from backend.app.migrations import m0010_recipes, m0011_recipe_defaulted
from backend.app.problems import RequestError
from backend.app.recipes import compile_recipe, merge_run, schema_for_fields


def test_compile_freezes_defaults_and_exposes_only_the_named_fields(client):
    compiled = compile_recipe(
        "image_local",
        {"prompt": "a ceramic cup", "seed": 7},
        ["prompt"],
        {},
        {"prompt": "What to draw."},
    )
    assert compiled["params"]["seed"] == 7
    assert compiled["params"]["prompt"] == "a ceramic cup"
    # A control the caller did not send is still frozen, so a later registry
    # default cannot retune this setup.
    assert "batch" in compiled["params"]
    assert [field["name"] for field in compiled["fields"]] == ["prompt"]
    assert compiled["fields"][0]["description"] == "What to draw."
    assert compiled["fields"][0]["default"] == "a ceramic cup"
    schema = schema_for_fields(compiled["fields"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["prompt"]["type"] == "string"
    assert schema["properties"]["prompt"]["description"] == "What to draw."
    assert "seed" not in schema["properties"]


def test_merge_refuses_a_frozen_setting_and_keeps_its_value(client):
    compiled = compile_recipe(
        "image_local", {"prompt": "a cup", "seed": 7}, ["prompt"], {},
    )
    params, inputs = merge_run(
        compiled["kind"], compiled["params"], compiled["inputs"], compiled["fields"],
        {"prompt": "a blue cup"}, compiled["defaulted"],
    )
    assert params["prompt"] == "a blue cup"
    assert params["seed"] == 7
    assert inputs["images"] == []
    with pytest.raises(RequestError) as caught:
        merge_run(
            compiled["kind"], compiled["params"], compiled["inputs"], compiled["fields"],
            {"steps": 4}, compiled["defaulted"],
        )
    error = caught.value
    assert error.code == "unknown_setting"
    assert error.field == "steps"
    assert error.valid_options == ["prompt"]


def test_exposed_images_with_an_empty_default_are_required(client):
    compiled = compile_recipe("inpaint", {"prompt": "repair the rim"}, ["images", "mask"], {})
    schema = schema_for_fields(compiled["fields"])
    assert schema["required"] == ["images", "mask"]
    images = next(field for field in compiled["fields"] if field["name"] == "images")
    assert images["constraints"]["min_items"] == 1
    assert images["default"] == []
    with pytest.raises(RequestError) as caught:
        merge_run(
            compiled["kind"], compiled["params"], compiled["inputs"], compiled["fields"],
            {"prompt": "nope"},
        )
    assert caught.value.field == "prompt"
    assert "images" in (caught.value.valid_options or [])


def test_a_hand_set_guidance_stays_when_the_model_changes(client):
    from backend.app.generators import variants

    offered = variants.available("local")
    source = offered[0]
    target = next(variant for variant in offered if variant.guidance != source.guidance)
    compiled = compile_recipe(
        "image_local",
        {"prompt": "a cup", "model_variant": source.name, "guidance": 2.5},
        ["model_variant"],
        {},
    )
    assert "guidance" not in compiled["defaulted"]
    params, _inputs = merge_run(
        compiled["kind"], compiled["params"], compiled["inputs"], compiled["fields"],
        {"model_variant": target.name}, compiled["defaulted"],
    )
    assert params["guidance"] == 2.5
    assert params["model_variant"] == target.name


def test_an_oversized_asset_id_is_refused(client):
    with pytest.raises(RequestError) as caught:
        compile_recipe(
            "upscale", {"scale": 2}, [], {"images": [10**18]},
        )
    assert caught.value.field == "inputs.images"
    compiled = compile_recipe("upscale", {"scale": 2}, ["images"], {})
    with pytest.raises(RequestError) as run:
        merge_run(
            compiled["kind"], compiled["params"], compiled["inputs"], compiled["fields"],
            {"images": [10**18]}, compiled["defaulted"],
        )
    assert run.value.field == "images"


def test_unknown_expose_and_a_slash_in_the_name_are_structured(client):
    with pytest.raises(RequestError) as unknown:
        compile_recipe("image_local", {"prompt": "a cup"}, ["promt"], {})
    assert unknown.value.code == "unknown_setting"
    assert unknown.value.field == "promt"
    assert "prompt" in (unknown.value.valid_options or [])
    from backend.app.recipes import clean_name

    with pytest.raises(RequestError) as slashed:
        clean_name("tea / cup")
    assert slashed.value.field == "name"


def test_recipe_migration_is_idempotent_and_indexes_the_name(tmp_path):
    path = tmp_path / "recipes.db"
    connection = sqlite3.connect(path)
    cursor = connection.cursor()
    # An earlier draft of 0010 created the table without defaulted_json.
    cursor.execute(
        """CREATE TABLE recipe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR NOT NULL DEFAULT '',
            slug VARCHAR NOT NULL DEFAULT '',
            description VARCHAR NOT NULL DEFAULT '',
            kind VARCHAR NOT NULL DEFAULT '',
            params_json VARCHAR NOT NULL DEFAULT '{}',
            fields_json VARCHAR NOT NULL DEFAULT '[]',
            inputs_json VARCHAR NOT NULL DEFAULT '{}',
            client VARCHAR NOT NULL DEFAULT '',
            created_at DATETIME,
            updated_at DATETIME
        )"""
    )
    m0010_recipes.run(cursor)
    m0010_recipes.run(cursor)
    connection.commit()
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(recipe)")}
    assert {
        "id", "name", "slug", "description", "kind", "params_json", "fields_json",
        "inputs_json", "defaulted_json", "client", "created_at", "updated_at",
    } <= columns
    cursor.execute("DROP TABLE recipe")
    m0010_recipes.run(cursor)
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(recipe)")}
    assert "defaulted_json" in columns
    indexes = {row[1]: row[2] for row in cursor.execute("PRAGMA index_list(recipe)")}
    assert indexes["ix_recipe_slug"] == 1
    assert indexes["ux_recipe_name_nocase"] == 1
    cursor.execute("INSERT INTO recipe (name, slug) VALUES ('Cup', 'cup')")
    with pytest.raises(sqlite3.IntegrityError):
        cursor.execute("INSERT INTO recipe (name, slug) VALUES ('cup', 'cup-2')")
    cursor.execute("DROP TABLE recipe")
    cursor.execute("CREATE TABLE recipe (id INTEGER PRIMARY KEY, name VARCHAR, slug VARCHAR)")
    m0011_recipe_defaulted.run(cursor)
    m0011_recipe_defaulted.run(cursor)
    assert "defaulted_json" in {row[1] for row in cursor.execute("PRAGMA table_info(recipe)")}
    connection.close()


def test_live_database_has_the_recipe_table(client):
    connection = sqlite3.connect(settings.db_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(recipe)")}
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(recipe)")}
    connection.close()
    assert "params_json" in columns and "fields_json" in columns and "defaulted_json" in columns
    assert {"ix_recipe_slug", "ux_recipe_name_nocase"} <= indexes
