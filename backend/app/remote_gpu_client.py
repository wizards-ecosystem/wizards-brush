"""HTTP client for the user-operated Remote GPU server (Qwen image + Wan video).

The remote service is reached at the URL saved in Settings. Every request carries
the shared-secret header. Video generation is long, so the timeout is generous.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import io
import json
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import ROOT, settings

# Control-plane calls must stay short even while a render is long. A single
# 90-second client timeout made ten failed polls hold the paid lane for roughly
# seventeen minutes before it admitted the tunnel was gone.
SUBMIT_TIMEOUT = 60.0
POLL_TIMEOUT = 15.0
CANCEL_TIMEOUT = 10.0
ACK_TIMEOUT = 10.0

# A configured worker is still a network peer: a stale, compromised, or
# misrouted endpoint must not be able to make the desktop process buffer an
# arbitrary response.  Image generation in this app tops out around 1.7 MP and
# eight images; Remote GPU video clips are short.  These ceilings leave ample
# room for those honest payloads while making memory and disk use finite.
MAX_CONTROL_RESPONSE_BYTES = 1024 * 1024
MAX_RESULT_RESPONSE_BYTES = 176 * 1024 * 1024
MAX_REMOTE_IMAGE_BYTES = 32 * 1024 * 1024
MAX_REMOTE_IMAGE_BATCH_BYTES = 128 * 1024 * 1024
MAX_REMOTE_VIDEO_BYTES = 128 * 1024 * 1024
MAX_PREVIEW_BYTES = 512 * 1024
MAX_REMOTE_IMAGE_SIDE = 16_384
MAX_REMOTE_IMAGE_PIXELS = 64_000_000
MAX_REMOTE_VIDEO_SIDE = 2048
MAX_REMOTE_VIDEO_PIXELS = 4_194_304
MAX_REMOTE_VIDEO_SECONDS = 60.0
MAX_REMOTE_VIDEO_FPS = 60.0
MAX_REMOTE_VIDEO_FRAMES = 180
MIN_REMOTE_DISK_RESERVE_BYTES = 128 * 1024 * 1024
_MAX_TOKEN_CHARS = 256


def _declared_length(response: httpx.Response, limit: int) -> None:
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding and encoding != "identity":
        raise RuntimeError("Remote GPU returned a compressed response")
    raw = response.headers.get("content-length")
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError as exc:
        raise RuntimeError("Remote GPU returned an invalid Content-Length") from exc
    if length < 0 or length > limit:
        raise RuntimeError(f"Remote GPU response exceeded the {limit // 1024 // 1024} MB limit")


def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    """Read a sync response while bounding declared and actual wire bytes."""
    _declared_length(response, limit)
    # Mock/custom transports may hand HTTPX an already-materialized response.
    # The production transport remains streamed; retain a defensive length
    # check for pre-read responses so tests and injected transports still fail
    # closed.
    if response.is_stream_consumed:
        if len(response.content) > limit:
            raise RuntimeError(
                f"Remote GPU response exceeded the {limit // 1024 // 1024} MB limit"
            )
        return response.content
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_raw():
        size += len(chunk)
        if size > limit:
            raise RuntimeError(
                f"Remote GPU response exceeded the {limit // 1024 // 1024} MB limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _aread_bounded(response: httpx.Response, limit: int) -> bytes:
    """Async counterpart of `_read_bounded`, including chunked responses."""
    _declared_length(response, limit)
    if response.is_stream_consumed:
        if len(response.content) > limit:
            raise RuntimeError(
                f"Remote GPU response exceeded the {limit // 1024 // 1024} MB limit"
            )
        return response.content
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_raw():
        size += len(chunk)
        if size > limit:
            raise RuntimeError(
                f"Remote GPU response exceeded the {limit // 1024 // 1024} MB limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _json_object(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Remote GPU returned invalid {what} JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Remote GPU returned invalid {what} data")
    return value


def _sync_request(
    client: httpx.Client, method: str, url: str, *, limit: int, **kwargs: Any,
) -> tuple[int, bytes]:
    request_headers = {**dict(kwargs.pop("headers", {}) or {}), "Accept-Encoding": "identity"}
    with client.stream(method, url, headers=request_headers, **kwargs) as response:
        return response.status_code, _read_bounded(response, limit)


def _encoded_limit(max_decoded_bytes: int) -> int:
    return ((max_decoded_bytes + 2) // 3) * 4


def decode_remote_base64(raw: Any, *, max_bytes: int, what: str) -> bytes:
    """Strictly decode one bounded base64 field received from the worker."""
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"Remote GPU returned an invalid {what}")
    if len(raw) > _encoded_limit(max_bytes):
        raise RuntimeError(f"Remote GPU {what} exceeded the size limit")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError(f"Remote GPU returned invalid {what} encoding") from exc
    if len(decoded) > max_bytes:
        raise RuntimeError(f"Remote GPU {what} exceeded the size limit")
    return decoded


def decode_remote_image(
    raw: Any, *, max_side: int = MAX_REMOTE_IMAGE_SIDE,
    max_pixels: int = MAX_REMOTE_IMAGE_PIXELS,
):
    """Validate a remote image's encoded bytes and header before full decode."""
    from PIL import Image, ImageOps

    data = decode_remote_base64(raw, max_bytes=MAX_REMOTE_IMAGE_BYTES, what="image")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            width, height = opened.size
            if (width <= 0 or height <= 0 or width > max_side or height > max_side
                    or width * height > max_pixels):
                raise RuntimeError("Remote GPU image dimensions exceeded the safe limit")
            opened.load()
            return ImageOps.exif_transpose(opened).convert("RGB")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Remote GPU returned an invalid image") from exc


def decode_remote_video(raw: Any) -> bytes:
    return decode_remote_base64(raw, max_bytes=MAX_REMOTE_VIDEO_BYTES, what="video")


def save_remote_video(raw: Any, destination: Path) -> Path:
    """Decode, reserve disk, write, and probe one worker-produced clip."""
    data = decode_remote_video(raw)
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination.parent).free
    if free < len(data) + MIN_REMOTE_DISK_RESERVE_BYTES:
        raise RuntimeError("Not enough free disk space to save the Remote GPU video")
    try:
        destination.write_bytes(data)
        del data
        from .utils.io import validate_video_file

        validate_video_file(
            destination,
            max_side=MAX_REMOTE_VIDEO_SIDE,
            max_pixels=MAX_REMOTE_VIDEO_PIXELS,
            max_seconds=MAX_REMOTE_VIDEO_SECONDS,
            max_fps=MAX_REMOTE_VIDEO_FPS,
            max_frames=MAX_REMOTE_VIDEO_FRAMES,
        )
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _check_encoded(raw: Any, max_bytes: int, what: str) -> int:
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"Remote GPU returned an invalid {what}")
    if len(raw) > _encoded_limit(max_bytes):
        raise RuntimeError(f"Remote GPU {what} exceeded the size limit")
    return len(raw)


def _validated_result(path: str, payload: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Validate response shape and fanout before acknowledging remote storage."""
    if not isinstance(raw, dict):
        raise RuntimeError("Remote GPU returned invalid result data")
    if path == "/image":
        images = raw.get("images_b64")
        if images is None:
            images = [raw.get("image_b64")]
        if not isinstance(images, list) or not images:
            raise RuntimeError("Remote GPU returned an invalid image result")
        prompts = payload.get("prompts")
        expected = len(prompts) if isinstance(prompts, list) and prompts else 1
        if len(images) > min(8, expected):
            raise RuntimeError("Remote GPU returned too many images")
        encoded_total = sum(_check_encoded(item, MAX_REMOTE_IMAGE_BYTES, "image")
                            for item in images)
        if encoded_total > _encoded_limit(MAX_REMOTE_IMAGE_BATCH_BYTES):
            raise RuntimeError("Remote GPU image batch exceeded the size limit")
    elif path == "/edit":
        _check_encoded(raw.get("image_b64"), MAX_REMOTE_IMAGE_BYTES, "image")
    elif path in {"/t2v", "/i2v"}:
        _check_encoded(raw.get("video_b64"), MAX_REMOTE_VIDEO_BYTES, "video")
    else:
        raise RuntimeError("Remote GPU returned a result for an unknown operation")
    return raw


def _preview_bytes(raw: Any) -> bytes | None:
    """The remote's latent preview as raw JPEG bytes.

    The Remote GPU worker sends it base64-encoded, because it travels inside a JSON poll
    response. Everything downstream — `progress_cb`, and the binary WebSocket
    frame it builds — speaks bytes, so the decode belongs here at the wire
    boundary rather than being pushed into the queue's fast path.

    Never raises: a malformed preview must cost a frame, not the job.
    """
    if not raw:
        return None
    if isinstance(raw, bytes | bytearray):
        return bytes(raw) if len(raw) <= MAX_PREVIEW_BYTES else None
    if not isinstance(raw, str):
        return None
    if len(raw) > _encoded_limit(MAX_PREVIEW_BYTES):
        return None
    with contextlib.suppress(Exception):
        decoded = base64.b64decode(raw, validate=True)
        return decoded if len(decoded) <= MAX_PREVIEW_BYTES else None
    return None


def _base_and_secret() -> tuple[str, str]:
    ov = settings.load_overrides()
    url = settings.effective_remote_gpu_url
    secret = (
        ov.get("remote_gpu_shared_secret")
        or ov.get("colab_shared_secret")
        or settings.remote_gpu_shared_secret
    )
    return url, secret


# Last successful /health payload, refreshed by every remote_gpu_health() call.
# The generator registry is built synchronously (GET /api/models) but needs to
# know what the remote GPU can actually do, so it reads this cache rather than
# guessing from the local .env — which is how the UI ended up offering speed
# mode against a session whose Lightning LoRA never loaded.
_LAST_HEALTH: dict = {"features": [], "connected": False}


def last_features() -> set[str]:
    """Features the live Remote GPU service reported, or empty if never reached.

    Empty means "unknown", not "unsupported": callers should fall back to the
    configured capability so a not-yet-connected session doesn't hide controls
    the user is about to need."""
    return set(_LAST_HEALTH.get("features") or [])


def last_health() -> dict:
    """The most recent /health payload, as a read-only snapshot.

    A copy, not the live dict. `_LAST_HEALTH` is mutated in place by the polling
    coroutine, so handing out the object itself would let a caller read a
    half-updated payload — or write to it. Three call sites in backends/remote_gpu.py
    reached across for the private name before this existed.
    """
    return dict(_LAST_HEALTH)


def remote_gpu_seen() -> bool:
    """True once a /health call has succeeded, so callers can tell 'unknown'
    apart from 'known to lack this feature'."""
    return bool(_LAST_HEALTH.get("connected"))


async def remote_gpu_health() -> dict:
    url, secret = _base_and_secret()
    if not url:
        _LAST_HEALTH.update(connected=False, features=[])
        return {"connected": False, "url": "", "reason": "no url set"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            async with c.stream(
                "GET", f"{url}/health",
                headers={"X-Gen-Secret": secret, "Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                data = await _aread_bounded(response, MAX_CONTROL_RESPONSE_BYTES)
            if status == 200:
                out = {**_json_object(data, "health"), "connected": True, "url": url}
                # Only claim staleness when the session actually reports a build.
                # A worker from before this existed reports nothing, and
                # "unknown" must not read as "out of date".
                remote_build = out.get("build") or ""
                out["build_stale"] = bool(remote_build) and remote_build != local_build_id()
                _LAST_HEALTH.clear()
                _LAST_HEALTH.update(out)
                return out
            _LAST_HEALTH.update(connected=False, features=[])
            return {"connected": False, "url": url, "reason": f"http {status}"}
    except Exception as e:  # noqa: BLE001
        _LAST_HEALTH.update(connected=False, features=[])
        return {"connected": False, "url": url, "reason": str(e)}



def local_build_id() -> str:
    """Fingerprint of this repo's remote_gpu.py, matching what `make remote_gpu` injects.

    Lets remote_gpu_health() tell whether the worker is running the current code.
    A stale worker is invisible otherwise: a fix shipped locally looks live
    while the remote GPU still executes the old build, and the only symptom is
    behaviour that contradicts the source you are reading."""
    import hashlib

    src = ROOT / "remote_gpu.py"
    try:
        return hashlib.sha256(src.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def _redact(text: str, secret: str) -> str:
    """Never echo the shared secret back into an error surfaced to the UI/logs."""
    return text.replace(secret, "***") if secret else text


def run_remote(
    path: str, payload: dict[str, Any], progress_cb: Callable[..., None] | None = None,
    *, poll: float = 3.0, timeout: float = 300.0, warmup_timeout: float = 1800.0,
    job_id: int | None = None,
) -> dict:
    """Submit a job to the Remote GPU server and poll until done. Returns the result dict.

    The Remote GPU POST enqueues and returns a token instantly; we then poll the short
    /result/{token} endpoint. This keeps every request well under Cloudflare's
    ~100s tunnel limit, so long generations/first-load downloads don't 524.

    `timeout` bounds silence after denoising begins, not total render time. Each
    genuine progress/status/preview change resets it. Cold model downloads have
    the separate `warmup_timeout`, because a large first load can be legitimate
    while an unchanged active render is a wedged worker.
    """
    url, secret = _base_and_secret()
    if not url:
        raise RuntimeError("Remote GPU is not connected — paste the URL in Settings.")
    headers = {"X-Gen-Secret": secret}
    # Idempotency key: a submit retry after a lost response must not enqueue the
    # job twice on the remote GPU (the server dedupes on this).
    client_id = uuid.uuid4().hex
    payload = {**payload, "client_job_id": client_id}
    if job_id is not None:
        from . import db

        # Written before submit: if the POST reaches Remote GPU but its response is
        # lost and this process dies, startup can still cancel by client id.
        db.set_remote_identity(job_id, client_id)

    def _post_with_retry(c: httpx.Client) -> str:
        last = None
        for attempt in range(5):
            try:
                status, data = _sync_request(
                    c, "POST", f"{url}{path}", limit=MAX_CONTROL_RESPONSE_BYTES,
                    json=payload, headers=headers, timeout=SUBMIT_TIMEOUT,
                )
                if status == 200:
                    token = _json_object(data, "submission").get("token")
                    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS:
                        raise RuntimeError("Remote GPU returned an invalid job token")
                    return token
                # 5xx / tunnel errors are worth retrying; 4xx (e.g. bad request) are not.
                if status < 500:
                    detail = data[:300].decode("utf-8", errors="replace")
                    raise RuntimeError(f"Remote GPU error {status}: {_redact(detail, secret)}")
                last = RuntimeError(f"Remote GPU error {status}")
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = RuntimeError(f"Remote GPU unreachable: {e}")
            if progress_cb:
                progress_cb(0.02, f"connecting to A100 (retry {attempt + 1}/5) …")
            time.sleep(min(2 ** attempt, 10))
        raise last or RuntimeError("Remote GPU POST failed")

    from .queue import CancelledJob, SkipItem

    with httpx.Client(timeout=SUBMIT_TIMEOUT) as c:
        token = _post_with_retry(c)
        if job_id is not None:
            from . import db

            db.set_remote_identity(job_id, client_id, token)

        def _cancel_remote() -> None:
            """Best-effort: tell the A100 to stop so a UI cancel actually frees it."""
            with contextlib.suppress(Exception):  # the A100 may already be gone
                _sync_request(
                    c, "POST", f"{url}/cancel/{token}", limit=MAX_CONTROL_RESPONSE_BYTES,
                    headers=headers, timeout=CANCEL_TIMEOUT,
                )

        def _ack_remote() -> None:
            """Tell the A100 we have the result so it can free it now.

            The server keeps a delivered result for a TTL precisely so a lost
            response can be re-polled, so skipping this only costs memory for a
            few minutes — never correctness."""
            with contextlib.suppress(Exception):
                _sync_request(
                    c, "POST", f"{url}/ack/{token}", limit=MAX_CONTROL_RESPONSE_BYTES,
                    headers=headers, timeout=ACK_TIMEOUT,
                )

        submitted_at = time.monotonic()
        last_activity = submitted_at
        miss = 0  # consecutive poll failures tolerated before giving up
        last_frac = 0.1  # keep the progress bar where it was through hiccups
        last_status = ""
        last_remote_progress = -1.0
        last_preview: Any = None
        active = False
        try:
            while True:
                try:
                    status_code, response_data = _sync_request(
                        c, "GET", f"{url}/result/{token}", limit=MAX_RESULT_RESPONSE_BYTES,
                        headers=headers, timeout=POLL_TIMEOUT,
                    )
                    # 5xx here is the tunnel hiccuping (cloudflared 502/504 HTML),
                    # not the job failing — retry like a transport error. Only 4xx
                    # (bad/unknown token) fails fast.
                    if status_code >= 500:
                        raise _PollHiccup(f"http {status_code}")
                    if status_code == 404:
                        raise RuntimeError(
                        "Remote GPU lost this job — the worker restarted or the "
                            "session expired. Restart the remote service and try again.")
                    if status_code != 200:
                        detail = response_data[:200].decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"Remote GPU result error {status_code}: {_redact(detail, secret)}")
                    d = _json_object(response_data, "result")
                    miss = 0
                except (httpx.TransportError, httpx.TimeoutException, _PollHiccup) as e:
                    miss += 1
                    if miss > 10:
                        raise RuntimeError(f"Remote GPU connection lost: {e}") from e
                    if progress_cb:
                        progress_cb(last_frac, "A100 link hiccup — retrying …")
                    time.sleep(min(poll * miss, 15))
                    continue
                now = time.monotonic()
                status = str(d.get("status", "running"))
                remote_progress = max(0.0, float(d.get("progress", 0.0)))
                preview = d.get("preview")
                changed = (status != last_status or remote_progress > last_remote_progress
                           or (preview is not None and preview != last_preview))
                if changed:
                    last_activity = now
                last_status = status
                last_remote_progress = max(last_remote_progress, remote_progress)
                if preview is not None:
                    last_preview = preview
                active = active or remote_progress > 0.0 or preview is not None
                if progress_cb:
                    last_frac = max(0.1, remote_progress)
                    progress_cb(last_frac, f"A100 {status}",
                                preview=_preview_bytes(preview))
                if status == "done":
                    result = _validated_result(path, payload, d.get("result"))
                    _ack_remote()   # result is in our memory now — let the A100 drop it
                    return result
                if status == "error":
                    # The A100 reports a cancelled job as an error rather than
                    # handing back the partially-denoised image it was holding.
                    # Surface that as cancellation, not as a failure, so the
                    # queue marks the job canceled instead of showing the user
                    # an error for something they asked for.
                    if str(d.get("error", "")).strip().lower() == "canceled":
                        raise CancelledJob
                    raise RuntimeError(f"Remote GPU job failed: {_redact(str(d.get('error')), secret)}")
                if active and now - last_activity >= timeout:
                    raise RuntimeError(
                        f"Remote GPU job timed out: render stalled with no progress for {timeout:g} seconds. "
                        "The remote worker may be wedged; reconnect or restart it.")
                if not active and now - submitted_at >= warmup_timeout:
                    raise RuntimeError(
                        f"Remote GPU job timed out: warm-up stalled after {warmup_timeout:g} seconds "
                        "without a first step. "
                        "Check the Remote GPU worker output for a model download or load failure.")
                time.sleep(poll)
        except (CancelledJob, SkipItem):
            _cancel_remote()
            raise
        except Exception:
            _cancel_remote()
            raise


def cancel_remote_identity(token: str | None, client_id: str | None) -> bool:
    """Best-effort restart reclamation for one persisted remote invocation."""
    url, secret = _base_and_secret()
    if not url or not (token or client_id):
        return False
    target = (f"/cancel/{quote(token, safe='')}" if token else
              f"/cancel/client/{quote(client_id or '', safe='')}")
    try:
        with httpx.Client(timeout=CANCEL_TIMEOUT) as c:
            status, data = _sync_request(
                c, "POST", f"{url}{target}", limit=MAX_CONTROL_RESPONSE_BYTES,
                headers={"X-Gen-Secret": secret}, timeout=CANCEL_TIMEOUT,
            )
        return status == 200 and bool(_json_object(data, "cancellation").get("ok"))
    except Exception:  # noqa: BLE001 — startup reclamation must not block boot
        return False


class _PollHiccup(Exception):
    """Transient non-200 from the tunnel while polling — retried, never fatal."""
