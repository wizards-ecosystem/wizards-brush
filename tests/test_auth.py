"""Opt-in API_TOKEN auth covers API, live events, and generated media."""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from backend.app.config import settings
from backend.app.security import browser_session_token


@pytest.fixture()
def token_on(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "tok123")
    yield "tok123"


def test_api_open_without_token(client):
    assert client.get("/api/stats").status_code == 200


def test_api_gated_with_token(client, token_on):
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/api/stats", headers={"X-API-Token": "tok123"}).status_code == 200
    # Header-only over HTTP: a ?token= query string would end up in access logs.
    assert client.get("/api/stats?token=tok123").status_code == 401


def test_browser_session_uses_server_set_http_only_cookies(client, token_on):
    path = settings.images_dir / "session_probe.png"
    response = client.post("/api/auth/session", headers={"X-API-Token": "tok123"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "authenticated": True}
    assert "tok123" not in response.text

    cookies = response.headers.get_list("set-cookie")
    api_cookie = next(value for value in cookies if value.startswith("wb_api_token="))
    media_cookie = next(value for value in cookies if value.startswith("wb_media_token="))
    assert "tok123" not in "\n".join(cookies)
    assert "HttpOnly" in api_cookie and "Path=/api" in api_cookie
    assert "HttpOnly" in media_cookie and "Path=/files" in media_cookie
    assert "SameSite=strict" in api_cookie and "SameSite=strict" in media_cookie
    assert response.headers["cache-control"] == "no-store"

    try:
        assert client.get("/api/stats").status_code == 200
        # Starlette's WebSocket test transport does not apply its cookie jar
        # automatically, so pass the server-issued API cookie to that request.
        with client.websocket_connect(
            "/api/jobs/ws",
            headers={
                "Host": "localhost",
                "Origin": "http://localhost",
                "Cookie": f"wb_api_token={client.cookies.get('wb_api_token')}",
            },
        ):
            pass

        path.write_bytes(b"probe")
        assert client.get("/files/images/session_probe.png").status_code == 200
        cleared = client.delete("/api/auth/session")
        assert cleared.json() == {"ok": True, "authenticated": False}
        assert client.get("/api/stats").status_code == 401
        assert client.get("/files/images/session_probe.png").status_code == 401
        # A stale or missing session must still be recoverable without opening
        # the endpoint to cross-site requests.
        assert client.delete("/api/auth/session").status_code == 200
        assert client.delete(
            "/api/auth/session", headers={"Origin": "https://evil.example"},
        ).status_code == 403
    finally:
        path.unlink(missing_ok=True)
        client.cookies.clear()


def test_non_api_paths_stay_open(client, token_on):
    # The SPA shell stays open so it can render the token form.
    r = client.get("/")
    assert r.status_code == 200


def test_generated_media_uses_a_path_scoped_cookie(client, token_on):
    from backend.app.config import settings

    path = settings.images_dir / "auth_probe.png"
    path.write_bytes(b"probe")
    try:
        assert client.get("/files/images/auth_probe.png").status_code == 401
        # A query token must not work: it would leak into logs and copied URLs.
        assert client.get("/files/images/auth_probe.png?token=tok123").status_code == 401
        client.cookies.set(
            "wb_media_token", browser_session_token("tok123", "media"), path="/files",
        )
        ok = client.get("/files/images/auth_probe.png")
        assert ok.status_code == 200 and ok.content == b"probe"
        assert ok.headers["cache-control"].startswith("private")
    finally:
        client.cookies.delete("wb_media_token", path="/files")
        path.unlink(missing_ok=True)


def test_browser_session_handles_percent_encoded_token_text(client, monkeypatch):
    token = "percent%2Fvalue"
    monkeypatch.setattr(settings, "api_token", token)
    response = client.post("/api/auth/session", headers={"X-API-Token": token})
    try:
        assert response.status_code == 200
        assert token not in "\n".join(response.headers.get_list("set-cookie"))
        assert client.get("/api/stats").status_code == 200
        with client.websocket_connect(
            "/api/jobs/ws",
            headers={
                "Host": "localhost",
                "Origin": "http://localhost",
                "Cookie": f"wb_api_token={client.cookies.get('wb_api_token')}",
            },
        ):
            pass
    finally:
        client.cookies.clear()


def test_browser_session_recovers_after_api_token_rotation(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "first-token")
    assert client.post(
        "/api/auth/session", headers={"X-API-Token": "first-token"},
    ).status_code == 200
    assert client.get("/api/stats").status_code == 200
    assert client.post(
        "/api/auth/session", headers={"X-API-Token": "wrong-token"},
    ).status_code == 401
    assert client.get("/api/stats").status_code == 200

    monkeypatch.setattr(settings, "api_token", "replacement-token")
    try:
        assert client.get("/api/stats").status_code == 401
        assert client.post(
            "/api/auth/session", headers={"X-API-Token": "replacement-token"},
        ).status_code == 200
        assert client.get("/api/stats").status_code == 200
    finally:
        client.cookies.clear()


def test_https_browser_session_cookies_are_secure(client, token_on):
    response = client.post(
        "https://localhost/api/auth/session", headers={"X-API-Token": "tok123"},
    )
    try:
        cookies = response.headers.get_list("set-cookie")
        assert all("Secure" in value for value in cookies)
    finally:
        client.cookies.clear()


def test_ws_open_without_token(client):
    with client.websocket_connect(
        "/api/jobs/ws", headers={"Host": "localhost", "Origin": "http://localhost"}
    ):
        pass  # accepted


def test_ws_gated_with_token(client, token_on):
    # The HTTP middleware never sees websocket scopes — the endpoint itself
    # must reject bad/missing tokens before accepting.
    headers = {"Host": "localhost", "Origin": "http://localhost"}
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/api/jobs/ws", headers=headers
    ):
        pass
    # Query-string credentials are ignored so secrets cannot leak into logs.
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/api/jobs/ws?token=tok123", headers=headers
    ):
        pass
    client.cookies.set("wb_ws_token", "tok123", path="/api/jobs/ws")
    with client.websocket_connect("/api/jobs/ws", headers=headers):
        pass  # accepted
    client.cookies.delete("wb_ws_token", path="/api/jobs/ws")


def test_ws_rejects_missing_or_foreign_origin(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/jobs/ws"):
        pass
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/api/jobs/ws", headers={"Origin": "https://evil.example"}
    ):
        pass


def test_tokenless_loopback_rejects_untrusted_host(client):
    assert client.get("/api/stats", headers={"Host": "evil.example"}).status_code == 421


def test_cross_origin_api_requests_are_rejected_but_cli_and_vite_work(client):
    endpoint = "/api/assets/bulk-delete"
    body: dict[str, list[int]] = {"ids": []}
    assert client.post(endpoint, json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(endpoint, json=body).status_code == 200
    assert client.post(
        endpoint, json=body, headers={"Origin": "http://localhost:5173"}
    ).status_code == 200
    assert client.get("/api/stats", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/stats", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/stats").status_code == 200
    assert client.get(
        "/api/stats", headers={"Origin": "http://localhost:5173"}
    ).status_code == 200


def test_responses_cannot_be_embedded(client):
    for path in ("/", "/api/stats"):
        response = client.get(path)
        assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert response.headers["x-frame-options"] == "DENY"


def test_cross_site_top_level_navigation_to_the_spa_still_works(client):
    response = client.get("/", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


# --- CORS ------------------------------------------------------------------
def test_cors_does_not_default_to_star(monkeypatch):
    """With API_TOKEN empty (the default), allow_origins=["*"] let any page the
    user had open POST /api/assets/bulk-delete at localhost and wipe the gallery.
    The SPA is same-origin and needs no grant, so the tight default costs nothing."""
    from backend.app.main import _allowed_origins

    assert "*" not in _allowed_origins()


def test_the_vite_dev_server_is_always_allowed():
    """`make dev` serves the SPA on 5173 against the API on 8000 — that is the
    one case CORS genuinely has to cover."""
    from backend.app.main import _allowed_origins

    allowed = _allowed_origins()
    assert "http://localhost:5173" in allowed
    assert "http://127.0.0.1:5173" in allowed


def test_extra_origins_can_be_configured(monkeypatch):
    from backend.app.config import settings
    from backend.app.main import _allowed_origins

    monkeypatch.setattr(settings, "cors_origins", "https://studio.example, http://box.lan:3000")
    allowed = set(_allowed_origins())
    assert {"https://studio.example", "http://box.lan:3000"}.issubset(allowed)
    assert not {
        "https://studio.example.evil.test",
        "https://studio.example@evil.test",
        "https://studio.example/path",
        "http://box.lan:3000.evil.test",
    }.intersection(allowed)
    assert "http://localhost:5173" in allowed   # dev origins still there


def test_star_is_ignored_even_when_configured(monkeypatch):
    """A wildcard is never a sound browser boundary for tokenless loopback mode."""
    from backend.app.config import settings
    from backend.app.main import _allowed_origins

    monkeypatch.setattr(settings, "cors_origins", "*")
    assert "*" not in _allowed_origins()
