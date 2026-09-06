"""remote_gpu_client contract tests over httpx.MockTransport: submit/poll protocol,
retry behavior, secret redaction, timeouts. No network, no torch."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import backend.app.remote_gpu_client as cc


def _mock_client_factory(monkeypatch, handler):
    """Route httpx.Client through a MockTransport; neutralize real sleeps."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(**kw):
        kw.pop("transport", None)
        return real_client(transport=transport, **kw)

    monkeypatch.setattr(cc.httpx, "Client", fake_client)
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)


def test_health_no_url():
    import asyncio

    from backend.app.config import settings

    prev = settings.load_overrides()
    settings.save_overrides({})
    try:
        h = asyncio.run(cc.remote_gpu_health())
        assert h == {"connected": False, "url": "", "reason": "no url set"}
    finally:
        settings.save_overrides(prev)


def test_run_remote_happy_path(monkeypatch, remote_gpu_env):
    seen = {"headers": [], "polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"].append(request.headers.get("X-Gen-Secret"))
        if request.url.path == "/image":
            assert json.loads(request.content)["prompt"] == "hi"
            return httpx.Response(200, json={"token": "t1"})
        assert request.url.path == "/result/t1"
        seen["polls"] += 1
        if seen["polls"] < 2:
            return httpx.Response(200, json={"status": "running", "progress": 0.5})
        return httpx.Response(200, json={"status": "done", "result": {"image_b64": "abc"}})

    _mock_client_factory(monkeypatch, handler)
    msgs = []
    out = cc.run_remote("/image", {"prompt": "hi"},
                        progress_cb=lambda f, m, **kw: msgs.append((f, m)))
    assert out == {"image_b64": "abc"}
    assert all(h == "s3cr3t-xyz" for h in seen["headers"])
    assert any("running" in m for _, m in msgs)


def test_run_remote_retries_5xx_then_succeeds(monkeypatch, remote_gpu_env):
    calls = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            calls["post"] += 1
            if calls["post"] < 3:
                return httpx.Response(502, text="tunnel hiccup")
            return httpx.Response(200, json={"token": "t2"})
        return httpx.Response(200, json={"status": "done", "result": {"image_b64": "eA=="}})

    _mock_client_factory(monkeypatch, handler)
    msgs = []
    out = cc.run_remote("/image", {}, progress_cb=lambda f, m, **kw: msgs.append(m))
    assert out == {"image_b64": "eA=="} and calls["post"] == 3
    assert any("retry" in m for m in msgs)


def test_run_remote_4xx_fails_fast(monkeypatch, remote_gpu_env):
    calls = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["post"] += 1
        return httpx.Response(403, text="bad secret s3cr3t-xyz")

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        cc.run_remote("/image", {})
    assert calls["post"] == 1  # 4xx is not retried
    assert "s3cr3t-xyz" not in str(exc.value)  # redacted
    assert "***" in str(exc.value)


def test_run_remote_error_status_redacts_secret(monkeypatch, remote_gpu_env):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t3"})
        return httpx.Response(200, json={"status": "error", "error": "boom secret=s3cr3t-xyz"})

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        cc.run_remote("/image", {})
    assert "Remote GPU job failed" in str(exc.value)
    assert "s3cr3t-xyz" not in str(exc.value)


def test_run_remote_poll_hiccups_then_gives_up(monkeypatch, remote_gpu_env):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t4"})
        raise httpx.ConnectError("tunnel down")

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Remote GPU connection lost"):
        cc.run_remote("/image", {})


def test_run_remote_timeout(monkeypatch, remote_gpu_env):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t5"})
        return httpx.Response(200, json={"status": "running", "progress": 0.1})

    _mock_client_factory(monkeypatch, handler)
    # Freeze poll sleeps to zero and shrink the deadline so this returns instantly.
    with pytest.raises(RuntimeError, match="timed out"):
        cc.run_remote("/image", {}, poll=0, timeout=0.05)


def test_run_remote_poll_5xx_is_retried(monkeypatch, remote_gpu_env):
    """A cloudflared 502/504 while polling is a tunnel hiccup, not a job failure."""
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t6"})
        polls["n"] += 1
        if polls["n"] < 3:
            return httpx.Response(502, text="<html>cloudflared error</html>")
        return httpx.Response(200, json={"status": "done", "result": {"image_b64": "eA=="}})

    _mock_client_factory(monkeypatch, handler)
    msgs = []
    assert cc.run_remote("/image", {}, progress_cb=lambda f, m, **kw: msgs.append(m)) == {
        "image_b64": "eA=="
    }
    assert any("hiccup" in m for m in msgs)


def test_cancel_during_poll_stops_the_a100(monkeypatch, remote_gpu_env):
    """A UI cancel raised from progress_cb must POST /cancel so the GPU stops."""
    from backend.app.queue import CancelledJob

    seen = {"cancel": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t7"})
        if request.url.path == "/cancel/t7":
            seen["cancel"] += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "running", "progress": 0.4})

    _mock_client_factory(monkeypatch, handler)

    def cb(f, m, **kw):
        raise CancelledJob()

    with pytest.raises(CancelledJob):
        cc.run_remote("/image", {}, progress_cb=cb)
    assert seen["cancel"] == 1


def test_timeout_cancels_remote(monkeypatch, remote_gpu_env):
    """Hitting the deadline must also free the A100, not just error locally."""
    seen = {"cancel": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t8"})
        if request.url.path == "/cancel/t8":
            seen["cancel"] += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "running", "progress": 0.1})

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="timed out"):
        cc.run_remote("/image", {}, poll=0, timeout=0.05)
    assert seen["cancel"] == 1


def test_submit_carries_idempotency_key(monkeypatch, remote_gpu_env):
    """Every submit carries a client_job_id so a retried POST can be deduped."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            seen["cid"] = json.loads(request.content).get("client_job_id")
            return httpx.Response(200, json={"token": "t9"})
        return httpx.Response(200, json={"status": "done", "result": {"image_b64": "eA=="}})

    _mock_client_factory(monkeypatch, handler)
    payload = {"prompt": "hi"}
    cc.run_remote("/image", payload)
    assert seen["cid"]
    assert "client_job_id" not in payload  # caller's dict not mutated


def test_remote_identity_is_persisted_before_and_after_submit(monkeypatch, remote_gpu_env):
    from backend.app import db

    db.init_db()
    job = db.create_job("image_colab", {"prompt": "x"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            seen["cid"] = json.loads(request.content)["client_job_id"]
            # The client id must already be durable before the response exists.
            during = db.get_job(job.id)
            assert during.remote_client_id == seen["cid"]
            assert during.remote_token is None
            return httpx.Response(200, json={"token": "durable-token"})
        if request.url.path == "/result/durable-token":
            return httpx.Response(200, json={"status": "done", "result": {"image_b64": "eA=="}})
        return httpx.Response(200, json={"ok": True})

    _mock_client_factory(monkeypatch, handler)
    assert cc.run_remote("/image", {}, job_id=job.id) == {"image_b64": "eA=="}
    stored = db.get_job(job.id)
    assert stored.remote_client_id == seen["cid"]
    assert stored.remote_token == "durable-token"


def test_watchdog_bounds_silence_not_total_render_time(monkeypatch, remote_gpu_env):
    clock = {"now": 0.0}
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "paced"})
        if request.url.path == "/result/paced":
            polls["n"] += 1
            if polls["n"] < 5:
                return httpx.Response(200, json={
                    "status": "running", "progress": polls["n"] / 10,
                })
            return httpx.Response(200, json={"status": "done", "result": {"image_b64": "eA=="}})
        return httpx.Response(200, json={"ok": True})

    _mock_client_factory(monkeypatch, handler)
    monkeypatch.setattr(cc.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cc.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    assert cc.run_remote("/image", {}, poll=2, timeout=3) == {"image_b64": "eA=="}
    assert clock["now"] > 3, "a total deadline would have killed this steadily advancing render"


def test_watchdog_stops_unchanged_active_render(monkeypatch, remote_gpu_env):
    clock = {"now": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "stalled"})
        if request.url.path == "/result/stalled":
            return httpx.Response(200, json={"status": "running", "progress": 0.2})
        return httpx.Response(200, json={"ok": True})

    _mock_client_factory(monkeypatch, handler)
    monkeypatch.setattr(cc.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cc.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    with pytest.raises(RuntimeError, match="render stalled"):
        cc.run_remote("/image", {}, poll=2, timeout=3)


def test_each_control_call_uses_its_own_timeout(monkeypatch, remote_gpu_env):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.extensions["timeout"]["read"]
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "timed"})
        if request.url.path == "/result/timed":
            return httpx.Response(200, json={"status": "done", "result": {"image_b64": "eA=="}})
        return httpx.Response(200, json={"ok": True})

    _mock_client_factory(monkeypatch, handler)
    cc.run_remote("/image", {})
    assert seen["/image"] == cc.SUBMIT_TIMEOUT
    assert seen["/result/timed"] == cc.POLL_TIMEOUT
    assert seen["/ack/timed"] == cc.ACK_TIMEOUT


def test_restart_reclamation_can_cancel_by_client_id(monkeypatch, remote_gpu_env):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    _mock_client_factory(monkeypatch, handler)
    assert cc.cancel_remote_identity(None, "client-before-token") is True
    assert seen == ["/cancel/client/client-before-token"]


# --- result-retention contract (the dropped-response bug) -------------------
def test_result_survives_a_dropped_response(monkeypatch, remote_gpu_env):
    """The A100 must keep a finished result until it is acked.

    Regression for the worst failure this client had: the server did
    `j.pop("result")` on the first read, so if the tunnel lost that one
    response — precisely what the retry loop exists to survive — every later
    poll 500'd and minutes of A100 time were unrecoverable. Here the first
    delivery is dropped in transit and the retry must still get the result.
    """
    state = {"delivered": 0, "acked": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t1"})
        if request.url.path == "/ack/t1":
            state["acked"] = True
            return httpx.Response(200, json={"ok": True})
        assert request.url.path == "/result/t1"
        state["delivered"] += 1
        if state["delivered"] == 1:
            # tunnel ate the response on the way back to us
            raise httpx.ReadTimeout("tunnel dropped the response")
        return httpx.Response(200, json={"status": "done", "result": {"image_b64": "abc"}})

    _mock_client_factory(monkeypatch, handler)
    out = cc.run_remote("/image", {"prompt": "hi"}, poll=0.01)

    assert out == {"image_b64": "abc"}      # recovered, not lost
    assert state["delivered"] == 2          # it really did take a second poll
    assert state["acked"] is True           # and we told the A100 to free it


def test_result_is_acked_so_the_a100_can_free_it(monkeypatch, remote_gpu_env):
    acked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t9"})
        if request.url.path == "/ack/t9":
            acked.append(True)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "done", "result": {"image_b64": "z"}})

    _mock_client_factory(monkeypatch, handler)
    assert cc.run_remote("/image", {"prompt": "p"}, poll=0.01) == {"image_b64": "z"}
    assert acked == [True]


def test_a_failed_ack_never_fails_the_job(monkeypatch, remote_gpu_env):
    """Ack is a courtesy — the result is already ours. Losing it must not throw."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "t2"})
        if request.url.path == "/ack/t2":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"status": "done", "result": {"image_b64": "ok"}})

    _mock_client_factory(monkeypatch, handler)
    assert cc.run_remote("/image", {"prompt": "p"}, poll=0.01) == {"image_b64": "ok"}


def test_lost_session_404_says_what_to_do(monkeypatch, remote_gpu_env):
    """A 404 mid-poll means the worker restarted — say so, don't leak a status code."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "gone"})
        return httpx.Response(404, json={"detail": "unknown token"})

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="worker restarted"):
        cc.run_remote("/image", {"prompt": "p"}, poll=0.01)


def test_a_remote_cancel_is_cancellation_not_failure(monkeypatch, remote_gpu_env):
    """diffusers' _interrupt makes the pipeline RETURN rather than raise, so the
    A100 finishes holding a partially-denoised image. It reports that as an
    error rather than passing off junk as a result — and the client has to read
    it as cancellation, or the user sees a red error for something they asked
    for."""
    from backend.app.queue import CancelledJob

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "c1"})
        if request.url.path == "/cancel/c1":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "error", "error": "canceled"})

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(CancelledJob):
        cc.run_remote("/image", {"prompt": "p"}, poll=0.01)


def test_a_real_remote_failure_is_still_an_error(monkeypatch, remote_gpu_env):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "c2"})
        return httpx.Response(200, json={"status": "error", "error": "CUDA out of memory"})

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="out of memory"):
        cc.run_remote("/image", {"prompt": "p"}, poll=0.01)


# --- build fingerprint -----------------------------------------------------
def test_health_flags_a_notebook_running_older_code(monkeypatch, remote_gpu_env):
    """A stale notebook is invisible otherwise: a fix shipped locally looks live
    while the A100 still runs the old build, and the only symptom is behaviour
    that contradicts the source you are reading."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "build": "deadbeefcafe", "features": []})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(cc.httpx, "AsyncClient",
                        lambda **kw: real(transport=transport, **{k: v for k, v in kw.items()
                                                                  if k != "transport"}))
    monkeypatch.setattr(cc, "local_build_id", lambda: "0123456789ab")
    assert asyncio.run(cc.remote_gpu_health())["build_stale"] is True


def test_matching_builds_are_not_flagged(monkeypatch, remote_gpu_env):
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "build": "0123456789ab", "features": []})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(cc.httpx, "AsyncClient",
                        lambda **kw: real(transport=transport, **{k: v for k, v in kw.items()
                                                                  if k != "transport"}))
    monkeypatch.setattr(cc, "local_build_id", lambda: "0123456789ab")
    assert asyncio.run(cc.remote_gpu_health())["build_stale"] is False


def test_a_session_without_a_build_marker_is_unknown_not_stale(monkeypatch, remote_gpu_env):
    """Notebooks from before the marker existed report nothing. Treating that as
    out-of-date would nag every such user with no way to satisfy it."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "features": []})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(cc.httpx, "AsyncClient",
                        lambda **kw: real(transport=transport, **{k: v for k, v in kw.items()
                                                                  if k != "transport"}))
    monkeypatch.setattr(cc, "local_build_id", lambda: "0123456789ab")
    assert asyncio.run(cc.remote_gpu_health())["build_stale"] is False


# ---- preview frames --------------------------------------------------------
#
# The remote lane sends its latent preview base64-encoded, because it rides
# inside a JSON poll response. Everything downstream speaks raw bytes: the
# queue's progress_cb hands the payload straight to wsframe.encode, which
# concatenates it onto a struct-packed header. A str there is a TypeError that
# fails the job — and it would have failed EVERY previewing Colab generation,
# because PREVIEW_EVERY defaults to 2.
def test_preview_is_decoded_to_bytes_at_the_wire_boundary():
    import base64

    from backend.app.remote_gpu_client import _preview_bytes

    raw = b"\xff\xd8\xff\xe0 not really a jpeg"
    assert _preview_bytes(base64.b64encode(raw).decode()) == raw


def test_a_decoded_preview_survives_the_binary_frame_encoder():
    """The exact composition that was broken: decode, then frame it."""
    import base64

    from backend.app import wsframe
    from backend.app.remote_gpu_client import _preview_bytes

    raw = b"\x00\x01\x02\x03"
    payload = _preview_bytes(base64.b64encode(raw).decode())
    frame = wsframe.encode(wsframe.FrameType.preview_jpeg, 42, payload)
    assert wsframe.decode(frame) == (wsframe.FrameType.preview_jpeg, 42, raw)


def test_a_missing_or_malformed_preview_costs_a_frame_not_the_job():
    from backend.app.remote_gpu_client import _preview_bytes

    assert _preview_bytes(None) is None
    assert _preview_bytes("") is None
    assert _preview_bytes("not valid base64 !!!") is None
    assert _preview_bytes(12345) is None
    assert _preview_bytes(b"already bytes") == b"already bytes"


def test_preview_size_is_bounded_before_decode(monkeypatch):
    import base64

    monkeypatch.setattr(cc, "MAX_PREVIEW_BYTES", 3)
    assert cc._preview_bytes(base64.b64encode(b"four").decode()) is None
    assert cc._preview_bytes(b"four") is None


def test_declared_oversized_poll_response_is_rejected_and_canceled(
    monkeypatch, remote_gpu_env,
):
    seen = {"cancel": 0}
    monkeypatch.setattr(cc, "MAX_RESULT_RESPONSE_BYTES", 16)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "bounded"})
        if request.url.path == "/cancel/bounded":
            seen["cancel"] += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, headers={"Content-Length": "17"}, content=b"")

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="response exceeded"):
        cc.run_remote("/image", {})
    assert seen["cancel"] == 1


def test_compressed_poll_response_is_rejected_before_decompression(
    monkeypatch, remote_gpu_env,
):
    import gzip

    seen = {"cancel": 0}
    compressed = gzip.compress(b"x" * (8 * 1024 * 1024))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "compressed"})
        if request.url.path == "/cancel/compressed":
            seen["cancel"] += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=compressed,
        )

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="compressed response"):
        cc.run_remote("/image", {})
    assert seen["cancel"] == 1


def test_chunked_oversized_poll_response_is_rejected(monkeypatch, remote_gpu_env):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"12345678"
            yield b"901234567"

    monkeypatch.setattr(cc, "MAX_RESULT_RESPONSE_BYTES", 16)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "chunked"})
        if request.url.path == "/cancel/chunked":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, stream=Chunks())

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="response exceeded"):
        cc.run_remote("/image", {})


def test_health_response_is_bounded(monkeypatch, remote_gpu_env):
    import asyncio

    monkeypatch.setattr(cc, "MAX_CONTROL_RESPONSE_BYTES", 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 17)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        cc.httpx,
        "AsyncClient",
        lambda **kw: real(
            transport=transport, **{key: value for key, value in kw.items() if key != "transport"}
        ),
    )
    health = asyncio.run(cc.remote_gpu_health())
    assert health["connected"] is False
    assert "response exceeded" in health["reason"]


def test_compressed_health_response_is_rejected(monkeypatch, remote_gpu_env):
    import asyncio
    import gzip

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=gzip.compress(b"x" * (8 * 1024 * 1024)),
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        cc.httpx,
        "AsyncClient",
        lambda **kw: real(
            transport=transport, **{key: value for key, value in kw.items() if key != "transport"}
        ),
    )
    health = asyncio.run(cc.remote_gpu_health())
    assert health["connected"] is False
    assert "compressed response" in health["reason"]


def test_result_fanout_cannot_exceed_the_submitted_batch(monkeypatch, remote_gpu_env):
    seen = {"cancel": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"token": "fanout"})
        if request.url.path == "/cancel/fanout":
            seen["cancel"] += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={
            "status": "done", "result": {"images_b64": ["eA==", "eA=="]},
        })

    _mock_client_factory(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="too many images"):
        cc.run_remote("/image", {"prompts": ["one"]})
    assert seen["cancel"] == 1


def test_remote_media_decoders_enforce_encoded_and_image_dimension_limits(monkeypatch):
    import base64
    import io

    from PIL import Image

    monkeypatch.setattr(cc, "MAX_REMOTE_VIDEO_BYTES", 3)
    with pytest.raises(RuntimeError, match="size limit"):
        cc.decode_remote_video(base64.b64encode(b"four").decode())

    buffer = io.BytesIO()
    Image.new("RGB", (65, 1)).save(buffer, format="PNG")
    with pytest.raises(RuntimeError, match="dimensions"):
        cc.decode_remote_image(
            base64.b64encode(buffer.getvalue()).decode(), max_side=64, max_pixels=64,
        )


def test_valid_remote_video_is_written_probed_and_keeps_disk_reserve(monkeypatch, tmp_path):
    import base64

    destination = tmp_path / "clip.mp4"
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        cc.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=cc.MIN_REMOTE_DISK_RESERVE_BYTES + 100),
    )
    monkeypatch.setattr(
        "backend.app.utils.io.validate_video_file",
        lambda path, **limits: seen.update(path=path, limits=limits),
    )
    assert cc.save_remote_video(base64.b64encode(b"video").decode(), destination) == destination
    assert destination.read_bytes() == b"video"
    assert seen["path"] == destination


def test_remote_video_refuses_a_write_that_would_consume_disk_reserve(monkeypatch, tmp_path):
    import base64

    destination = tmp_path / "clip.mp4"
    monkeypatch.setattr(
        cc.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=cc.MIN_REMOTE_DISK_RESERVE_BYTES + 4),
    )
    with pytest.raises(RuntimeError, match="free disk space"):
        cc.save_remote_video(base64.b64encode(b"video").decode(), destination)
    assert not destination.exists()


def test_video_probe_rejects_dimensions_before_reading_a_frame(monkeypatch, tmp_path):
    import sys

    from backend.app.utils.io import validate_video_file

    class MetadataOnly:
        def __init__(self):
            self.reads = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.reads += 1
            if self.reads == 1:
                return {"source_size": (8192, 8192), "duration": 2.0, "fps": 20.0}
            raise AssertionError("a frame was decoded before dimensions were accepted")

        def close(self):
            return None

    reader = MetadataOnly()
    monkeypatch.setitem(
        sys.modules, "imageio_ffmpeg", SimpleNamespace(read_frames=lambda _path: reader),
    )
    with pytest.raises(RuntimeError, match="dimensions"):
        validate_video_file(
            tmp_path / "untrusted.mp4", max_side=4096,
            max_pixels=16_777_216, max_seconds=60.0, max_fps=60.0, max_frames=180,
        )
    assert reader.reads == 1


@pytest.mark.parametrize(
    "metadata,match",
    [
        ({"source_size": (1280, 704), "fps": 20.0}, "metadata"),
        ({"source_size": (1280, 704), "duration": float("nan"), "fps": 20.0}, "duration"),
        ({"source_size": (1280, 704), "duration": 2.0, "fps": 1_000_000.0}, "frame rate"),
        ({"source_size": (1280, 704), "duration": 10.0, "fps": 30.0}, "frame count"),
    ],
)
def test_video_probe_rejects_unbounded_timing_metadata(
    monkeypatch, tmp_path, metadata, match,
):
    import sys

    from backend.app.utils.io import validate_video_file

    class Reader:
        def __iter__(self):
            return self

        def __next__(self):
            return metadata

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules, "imageio_ffmpeg", SimpleNamespace(read_frames=lambda _path: Reader()),
    )
    with pytest.raises(RuntimeError, match=match):
        validate_video_file(
            tmp_path / "untrusted.mp4", max_side=2048, max_pixels=4_194_304,
            max_seconds=60.0, max_fps=60.0, max_frames=180,
        )


def test_derived_video_reserves_aggregate_output_space(monkeypatch, tmp_path):
    from backend.app.utils import io as io_utils

    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(b"1" * 10)
    second.write_bytes(b"2" * 10)
    required = 64 * 1024 * 1024 + 128 * 1024 * 1024
    monkeypatch.setattr(io_utils.shutil, "disk_usage", lambda _path: SimpleNamespace(free=required - 1))
    with pytest.raises(RuntimeError, match="free disk space"):
        io_utils.require_video_output_space(tmp_path, [first, second])
