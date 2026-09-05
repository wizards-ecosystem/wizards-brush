"""Image IO helpers: save/thumb/fit/load. Pillow-only — no ffmpeg/video here."""
from __future__ import annotations

import io

from PIL import Image

from backend.app.config import settings
from backend.app.utils.io import fit_image, load_image_bytes, make_thumb, save_image


def test_save_image_writes_png_and_thumb():
    img = Image.new("RGB", (640, 480), (10, 200, 30))
    saved = save_image(img, tag="t_io")
    assert saved.path.exists() and saved.path.suffix == ".png"
    assert (saved.width, saved.height) == (640, 480)
    assert saved.thumb and (settings.thumbs_dir / saved.thumb).exists()


def test_save_image_hashes_what_it_wrote():
    """The hash must be of the bytes on disk, not of anything re-encoded after."""
    from backend.app.utils.hashing import hash_file

    img = Image.new("RGB", (32, 32), (7, 7, 7))
    saved = save_image(img, tag="t_hash")
    assert saved.content_hash == hash_file(saved.path)
    assert saved.size_bytes == saved.path.stat().st_size


def test_identical_images_hash_identically():
    """Dedup depends on this: same pixels, saved twice, one content identity."""
    a = save_image(Image.new("RGB", (16, 16), (1, 2, 3)), tag="t_dupe_a")
    b = save_image(Image.new("RGB", (16, 16), (1, 2, 3)), tag="t_dupe_b")
    assert a.content_hash == b.content_hash
    assert a.path != b.path, "two files, one content"


def test_different_images_hash_differently():
    a = save_image(Image.new("RGB", (16, 16), (1, 2, 3)), tag="t_diff_a")
    b = save_image(Image.new("RGB", (16, 16), (3, 2, 1)), tag="t_diff_b")
    assert a.content_hash != b.content_hash


def test_make_thumb_max_side():
    img = Image.new("RGB", (2000, 1000))
    name = make_thumb(img, "t_thumb_src.png")
    t = Image.open(settings.thumbs_dir / name)
    assert max(t.size) <= 384


def test_fit_image_exact_dims_cover():
    # Wider source: cover-fit crops the sides, never distorts.
    src = Image.new("RGB", (400, 100), (0, 0, 255))
    out = fit_image(src, 100, 100)
    assert out.size == (100, 100)
    # Taller source.
    src = Image.new("RGB", (100, 400))
    assert fit_image(src, 100, 100).size == (100, 100)
    # Equal aspect: a solid color survives the resize untouched.
    src = Image.new("RGB", (200, 200), (7, 8, 9))
    out = fit_image(src, 100, 100)
    assert out.size == (100, 100) and out.getpixel((50, 50)) == (7, 8, 9)


def test_load_image_bytes_converts_rgb():
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (1, 2, 3, 255)).save(buf, "PNG")
    img = load_image_bytes(buf.getvalue())
    assert img.mode == "RGB" and img.size == (4, 4)
