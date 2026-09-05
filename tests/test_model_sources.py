"""Shipped Hub defaults resolve to reviewed immutable commits."""
from __future__ import annotations

from backend.app.config import Settings
from backend.app.generators import local_image
from backend.app.model_sources import MODEL_REVISIONS, hub_revision


def test_every_enabled_public_default_has_a_commit_revision():
    fields = (
        "local_image_model", "a100_image_model", "a100_image_model_hidream",
        "qwen_edit_model", "video_model", "image_lightning_lora",
        "edit_lightning_lora", "embed_model", "controlnet_model",
    )
    defaults = [str(Settings.model_fields[name].default or "") for name in fields]
    missing = [repo for repo in defaults if repo and repo not in MODEL_REVISIONS]
    assert not missing
    assert all(len(hub_revision(repo)["revision"]) == 40 for repo in defaults if repo)


def test_custom_model_id_remains_an_explicit_operator_choice():
    assert hub_revision("operator/private-model") == {}


def test_local_prefetch_passes_the_reviewed_revision_to_download():
    source = __import__("inspect").getsource(local_image._prefetch_model)
    assert "revision=revision_for(model)" in source
