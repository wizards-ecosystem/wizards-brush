"""Live preview work is demand-driven while scalar progress always continues."""
from __future__ import annotations

from backend.app.generators import local_image, preview
from backend.app.queue import hub


def test_preview_is_not_built_without_a_websocket_subscriber(monkeypatch):
    monkeypatch.setattr(local_image.settings, "preview_every", 1)
    calls: list[int] = []
    progress: list[tuple[float, str, dict[str, object]]] = []

    def build_preview(*_args, **_kwargs) -> bytes:
        calls.append(1)
        return b"jpeg"

    monkeypatch.setattr(preview, "latents_to_jpeg", build_preview)
    callback = local_image._callback(
        lambda fraction, message, **kw: progress.append((fraction, message, kw)),
        steps=3, width=512, height=512, model="test",
    )

    callback(object(), 0, None, {"latents": object()})
    assert calls == []
    assert progress[0][0] == 1 / 3
    assert progress[0][2]["preview"] is None


def test_preview_is_built_when_a_client_is_connected(monkeypatch):
    monkeypatch.setattr(local_image.settings, "preview_every", 1)
    monkeypatch.setattr(preview, "latents_to_jpeg", lambda *a, **k: b"jpeg")
    got: list[bytes | None] = []
    subscriber = hub.subscribe()
    try:
        callback = local_image._callback(
            lambda fraction, message, **kw: got.append(kw.get("preview")),
            steps=3, width=512, height=512, model="test",
        )
        callback(object(), 0, None, {"latents": object()})
    finally:
        hub.unsubscribe(subscriber)
    assert got == [b"jpeg"]
