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
