"""First-use tool downloads are authenticated before native/model loaders see them."""
from __future__ import annotations

import hashlib

import pytest

from backend.app.generators import postprocess


def test_download_accepts_only_the_expected_digest(tmp_path, monkeypatch):
    payload = b"reviewed weight bytes"
    expected = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(postprocess, "WEIGHTS", tmp_path)
    monkeypatch.setattr(
        postprocess.urllib.request, "urlretrieve",
        lambda _url, destination: destination.write_bytes(payload),
    )
    path = postprocess._download("https://weights.example/model", "model.bin", expected)
    assert path.read_bytes() == payload


def test_tampered_cached_weight_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(postprocess, "WEIGHTS", tmp_path)
    (tmp_path / "model.bin").write_bytes(b"tampered")
    with pytest.raises(postprocess.ToolUnavailable, match="SHA-256"):
        postprocess._download("https://weights.example/model", "model.bin", "0" * 64)
