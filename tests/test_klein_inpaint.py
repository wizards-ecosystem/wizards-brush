"""Local inpaint class resolution: Klein uses the family InpaintPipeline."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from backend.app.generators import local_image


def test_klein_inpaint_resolves_to_the_family_pipeline(monkeypatch, tmp_path):
    idx = tmp_path / "model_index.json"
    idx.write_text(json.dumps({"_class_name": "Flux2KleinPipeline"}))
    monkeypatch.setattr(local_image, "hf_hub_download", lambda *a, **k: str(idx))

    class Flux2KleinPipeline:
        pass

    class Flux2KleinInpaintPipeline:
        pass

    fake = SimpleNamespace(
        Flux2KleinPipeline=Flux2KleinPipeline,
        Flux2KleinInpaintPipeline=Flux2KleinInpaintPipeline,
    )
    monkeypatch.setitem(sys.modules, "diffusers", fake)
    try:
        _base, _img2img, inpaint = local_image.resolve_classes("unused/repo")
        assert inpaint is Flux2KleinInpaintPipeline
    finally:
        sys.modules.pop("diffusers", None)
