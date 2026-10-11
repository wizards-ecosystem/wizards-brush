"""SCHP/BiSeNet region masks: tests inject a predictor and never download weights."""
from __future__ import annotations

from PIL import Image

from backend.app.generators import parsing


def _paint(img: Image.Image, box, value: int) -> None:
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            img.putpixel((x, y), value)


def _atr_labels(img: Image.Image) -> Image.Image:
    labels = Image.new("L", img.size, parsing.ATR_INDEX["background"])
    _paint(labels, (0, 0, 16, 16), parsing.ATR_INDEX["face"])
    _paint(labels, (16, 0, 48, 16), parsing.ATR_INDEX["hair"])
    _paint(labels, (8, 16, 40, 48), parsing.ATR_INDEX["upper-clothes"])
    return labels


def test_upper_clothes_mask_is_not_a_face_box(monkeypatch):
    monkeypatch.setattr(parsing, "_PREDICT", lambda kind, img: _atr_labels(img))
    img = Image.new("RGB", (48, 48), "navy")
    mask = parsing.mask_for(img, "upper-clothes", grow=0)
    assert mask.getpixel((8, 8)) == 0, "face must not be in the clothing mask"
    assert mask.getpixel((24, 32)) == 255, "upper-clothes pixels must be white"


def test_hair_mask_covers_hair_not_the_torso(monkeypatch):
    monkeypatch.setattr(parsing, "_PREDICT", lambda kind, img: _atr_labels(img))
    img = Image.new("RGB", (48, 48), "navy")
    mask = parsing.mask_for(img, "hair", grow=0)
    assert mask.getpixel((24, 8)) == 255
    assert mask.getpixel((24, 32)) == 0


def test_click_maps_atr_class_onto_public_regions(monkeypatch):
    monkeypatch.setattr(parsing, "_PREDICT", lambda kind, img: _atr_labels(img))
    img = Image.new("RGB", (48, 48), "navy")
    assert parsing.region_at(img, 24, 8) == "hair"
    assert parsing.region_at(img, 24, 32) == "upper-clothes"
    assert parsing.region_at(img, 8, 8) == "face"


def test_predictors_send_float32_nchw_to_onnx():
    """Real numeric regression in a child process; CI needs no imaging stack."""
    import importlib.util
    import subprocess
    import sys
    import textwrap

    import pytest

    if importlib.util.find_spec("numpy") is None:
        pytest.skip("numeric preprocessing check needs the optional imaging environment")
    code = textwrap.dedent('''
        from types import SimpleNamespace
        import numpy as np
        from PIL import Image
        from backend.app.generators import parsing

        image = Image.new("RGB", (21, 35), (192, 96, 32))
        parsing.unavailable_reason = lambda: None

        class Session:
            def __init__(self, kind):
                self.kind = kind

            def get_inputs(self):
                return [SimpleNamespace(name="pixels")]

            def run(self, output_names, inputs):
                batch = inputs["pixels"]
                assert batch.dtype == np.float32, f"ONNX expected float32, got {batch.dtype}"
                assert batch.shape == (1, 3, 512, 512)
                assert batch.flags.c_contiguous
                assert np.isfinite(batch).all()
                if self.kind == "schp":
                    rgb = (32, 96, 192)
                    mean, std = parsing.SCHP_MEAN, parsing.SCHP_STD
                    channels = len(parsing.ATR_CLASSES)
                else:
                    rgb = (192, 96, 32)
                    mean, std = parsing.BISENET_MEAN, parsing.BISENET_STD
                    channels = len(parsing.FACE_CLASSES)
                expected = [(value / 255.0 - m) / s for value, m, s in zip(rgb, mean, std)]
                np.testing.assert_allclose(batch[0, :, 0, 0], expected, rtol=1e-6, atol=1e-6)
                return [np.zeros((1, channels, 32, 32), dtype=np.float32)]

        parsing._session = Session
        for predict in (parsing.predict_atr, parsing.predict_face):
            labels = predict(image)
            assert labels.mode == "L"
            assert labels.size == image.size
            assert labels.getextrema() == (0, 0)
    ''')
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
