"""File naming, saving, and thumbnailing for images and videos."""
from __future__ import annotations

import contextlib
import io
import itertools
import math
import shutil
import time
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageOps

from .. import log
from ..config import settings

logger = log.get("io")

# itertools.count is atomic in CPython — both lanes' threads save concurrently,
# and a shared `n += 1` could hand two same-ms saves the same name.
_counter = itertools.count(1)


def stamp_name(tag: str, ext: str) -> str:
    """Collision-free filename: wall-clock ms (unique across restarts — monotonic
    resets per boot and could collide) + an in-process counter for same-ms saves."""
    return f"{tag}_{int(time.time() * 1000)}_{next(_counter):04d}.{ext}"


class SavedImage(NamedTuple):
    """What a save produced. A tuple so existing unpacking of the first four
    fields keeps working, with the content identity appended."""
    path: Path
    thumb: str
    width: int
    height: int
    content_hash: str
    size_bytes: int
    metadata_error: str


# ---- images ---------------------------------------------------------------
def save_image(img: Image.Image, tag: str = "img", *,
               meta: dict | None = None, kind: str = "") -> SavedImage:
    """Save a PIL image + a thumbnail, hashing the bytes as they are written.

    The PNG is encoded to memory first so the same bytes can be hashed and
    written in one pass. That costs the peak memory of one encoded image — which
    we were already paying inside Pillow's writer — and buys a content hash with
    no second read of the file off disk.

    `meta` and `kind`, when given, are embedded as text chunks: A1111-style
    `parameters` for interoperability and an XMP packet declaring the image as
    machine-generated. Both travel with the file once it leaves the gallery,
    which the database cannot.
    """
    import io as _io

    from .hashing import write_and_hash

    settings.ensure_dirs()
    name = stamp_name(tag, "png")
    path = settings.images_dir / name
    buf = _io.BytesIO()
    pnginfo, metadata_error = _png_info(meta, kind)
    img.save(buf, format="PNG", pnginfo=pnginfo)
    data = buf.getvalue()
    digest = write_and_hash(data, path)
    thumb = make_thumb(img, name)
    return SavedImage(
        path, thumb, img.width, img.height, digest, len(data), metadata_error)


def _png_info(meta: dict | None, kind: str) -> tuple[object | None, str]:
    """(Pillow PngInfo, error) carrying provenance and interop chunks.

    Metadata remains non-fatal, but failure is not invisible: the error is
    logged here and returned so the database row can warn that this particular
    file does not carry its otherwise-expected portable recipe/provenance.
    """
    if not meta:
        return None, ""
    try:
        from PIL.PngImagePlugin import PngInfo

        from ..metadata import png_text_chunks

        info = PngInfo()
        for key, value in png_text_chunks(
            meta, kind,
            include_metadata=settings.effective_bool("embed_metadata"),
            include_provenance=settings.effective_bool("embed_provenance"),
        ).items():
            info.add_text(key, value)
        return info, ""
    except Exception as error:  # noqa: BLE001 — never let metadata block a save
        logger.warning("PNG metadata embedding failed for %s: %s", kind or "image", error)
        return None, type(error).__name__


def make_thumb(img: Image.Image, source_name: str, size: int = 384) -> str:
    t = img.copy().convert("RGB")
    t.thumbnail((size, size), Image.Resampling.LANCZOS)
    thumb_name = f"{Path(source_name).stem}.jpg"
    t.save(settings.thumbs_dir / thumb_name, format="JPEG", quality=85)
    return thumb_name


def load_image_bytes(data: bytes) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")


def fit_image(img: Image.Image, w: int, h: int) -> Image.Image:
    """Center-crop cover-fit to exactly (w, h)."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    nw, nh = round(sw * scale), round(sh * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


# ---- videos ---------------------------------------------------------------
def validate_video_file(
    path: Path, *, max_side: int, max_pixels: int, max_seconds: float,
    max_fps: float, max_frames: int,
) -> tuple[int, int]:
    """Probe an untrusted video container before any frame is decoded.

    `read_frames` yields ffmpeg's metadata first. Closing immediately avoids a
    frame allocation, letting callers reject hostile dimensions or duration
    before thumbnail and last-frame helpers start decoding.
    """
    import imageio_ffmpeg

    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        meta = next(reader)
    except Exception as exc:
        raise RuntimeError("Remote GPU returned an invalid video") from exc
    finally:
        with contextlib.suppress(Exception):
            reader.close()

    raw_size = meta.get("source_size") or meta.get("size")
    try:
        width, height = int(raw_size[0]), int(raw_size[1])
        duration = float(meta["duration"])
        fps = float(meta["fps"])
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RuntimeError("Remote GPU returned invalid video metadata") from exc
    if (width <= 0 or height <= 0 or width > max_side or height > max_side
            or width * height > max_pixels):
        raise RuntimeError("Remote GPU video dimensions exceeded the safe limit")
    if not math.isfinite(duration) or duration <= 0 or duration > max_seconds:
        raise RuntimeError("Remote GPU video duration exceeded the safe limit")
    if not math.isfinite(fps) or fps <= 0 or fps > max_fps:
        raise RuntimeError("Remote GPU video frame rate exceeded the safe limit")
    if math.ceil(duration * fps) > max_frames:
        raise RuntimeError("Remote GPU video frame count exceeded the safe limit")
    return width, height


def require_video_output_space(
    directory: Path, sources: list[Path], *, multiplier: int = 4,
    reserve_bytes: int = 128 * 1024 * 1024,
) -> None:
    """Reserve space for a derived video before an encoder starts writing.

    Re-encoding can expand a highly compressed input. Four times the aggregate
    source size plus a fixed reserve is intentionally conservative for the
    short, bounded clips accepted from the Remote GPU worker.
    """
    source_bytes = sum(path.stat().st_size for path in sources if path.exists())
    required = max(64 * 1024 * 1024, source_bytes * multiplier) + reserve_bytes
    if shutil.disk_usage(directory).free < required:
        raise RuntimeError("Not enough free disk space for video processing")


def last_frame_png_b64(src: bytes | Path) -> str:
    """Base64 form of last_frame_png — for callers shipping it in a JSON body."""
    import base64

    return base64.b64encode(last_frame_png(src)).decode()


def last_frame_png(src: bytes | Path) -> bytes:
    """Return the last frame of an mp4 clip as PNG bytes (for I2V chaining).
    Accepts raw bytes or a path — pass the path when the file is already on disk
    so a 200 MB video isn't round-tripped through RAM and a temp copy."""
    import tempfile

    import imageio

    if isinstance(src, Path):
        tmp, own_tmp = str(src), False
    else:
        settings.ensure_dirs()
        with tempfile.NamedTemporaryFile(
            suffix=".mp4", delete=False, dir=settings.temp_path
        ) as f:
            f.write(src)
            tmp, own_tmp = f.name, True
    try:
        reader = imageio.get_reader(tmp, "ffmpeg")  # type: ignore[arg-type]
        frame = None
        try:
            # Fast path: jump straight to the last frame when the count is known.
            try:
                n = reader.count_frames()  # type: ignore[attr-defined]
                if n and n > 0:
                    frame = reader.get_data(n - 1)
            except Exception:  # noqa: BLE001 — some streams report no length; fall back
                frame = None
            if frame is None:
                for frame in reader.iter_data():  # noqa: B007
                    pass
        finally:
            reader.close()  # even on a decode error — leaks an ffmpeg subprocess otherwise
        if frame is None:
            raise ValueError("video has no decodable frames")
        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    finally:
        if own_tmp:
            Path(tmp).unlink(missing_ok=True)


def first_frame(path: Path) -> Image.Image:
    """First frame of a video via imageio's bundled ffmpeg (no pyav dep). The
    single home for this idiom — the reader is closed even on a decode failure
    (it leaks an ffmpeg subprocess otherwise)."""
    import imageio

    reader = imageio.get_reader(str(path), "ffmpeg")  # type: ignore[arg-type]
    try:
        frame = reader.get_data(0)
    finally:
        reader.close()
    return Image.fromarray(frame)


def video_thumb(path: Path, source_name: str) -> tuple[str | None, int, int]:
    """First-frame poster + dimensions."""
    try:
        img = first_frame(path)
        thumb = make_thumb(img, source_name)
        return thumb, img.width, img.height
    except Exception as e:  # noqa: BLE001
        logger.warning("video thumb failed: %s", e)
        return None, 0, 0
