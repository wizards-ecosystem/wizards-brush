"""The SPA shell must revalidate; its hashed assets must not.

index.html's name never changes and its only job is to name the current bundle.
Served with just ETag/Last-Modified — FileResponse's default — a browser may
reuse it without asking, so a plain refresh keeps loading the PREVIOUS bundle
and a deployed change looks like it never shipped. That is invisible from the
server side, which is why it is pinned here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def _dist_exists() -> bool:
    from backend.app.main import _DIST

    return _DIST.exists()


@pytest.mark.skipif(not _dist_exists(), reason="frontend not built")
def test_shell_is_revalidated_not_blindly_reused(client):
    for path in ("/", "/g/image_local", "/gallery"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache", path


@pytest.mark.skipif(not _dist_exists(), reason="frontend not built")
def test_hashed_assets_are_immutable(client):
    from backend.app.main import _DIST

    js = next((p for p in (_DIST / "assets").glob("index-*.js")), None)
    if js is None:
        pytest.skip("no built bundle")
    r = client.get(f"/assets/{js.name}")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "immutable" in cc and "max-age=31536000" in cc


def test_api_responses_are_not_marked_no_cache(client):
    """The rule is for the SPA shell. Tagging API JSON would be wrong — those
    responses are already dynamic and some are polled hard."""
    r = client.get("/api/models")
    assert r.status_code == 200
    assert r.headers.get("cache-control") != "no-cache"
