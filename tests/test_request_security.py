"""Pure-ASGI checks for limits that must run before request parsing."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app.security import BrowserSecurityMiddleware


def _scope(
    headers: list[tuple[bytes, bytes]], *, method: str = "POST", path: str = "/api/upload"
) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "scheme": "http",
        "headers": [(b"host", b"localhost"), *headers],
    }


def test_content_length_is_rejected_without_consuming_body():
    calls = {"app": 0, "receive": 0}
    sent: list[dict] = []

    async def app(scope, receive, send):
        calls["app"] += 1

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"12345", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = BrowserSecurityMiddleware(
        app, settings=SimpleNamespace(api_token="", cors_origins="", effective_bool=lambda _: True),
        max_body_bytes=4,
    )
    asyncio.run(middleware(_scope([(b"content-length", b"5")]), receive, send))
    assert sent[0]["status"] == 413
    assert calls == {"app": 0, "receive": 0}


def test_chunked_body_is_stopped_at_the_same_limit():
    sent: list[dict] = []
    chunks = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"45", "more_body": False},
    ])

    async def app(scope, receive, send):
        await receive()
        await receive()

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    middleware = BrowserSecurityMiddleware(
        app, settings=SimpleNamespace(api_token="", cors_origins="", effective_bool=lambda _: True),
        max_body_bytes=4,
    )
    asyncio.run(middleware(_scope([]), receive, send))
    assert sent[0]["status"] == 413


def test_cross_site_api_get_is_rejected_before_routing():
    calls = {"app": 0}
    sent: list[dict] = []

    async def app(scope, receive, send):
        calls["app"] += 1

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = BrowserSecurityMiddleware(
        app, settings=SimpleNamespace(api_token="", cors_origins="", effective_bool=lambda _: True)
    )
    asyncio.run(middleware(
        _scope([(b"sec-fetch-site", b"cross-site")], method="GET", path="/api/assets"),
        receive,
        send,
    ))
    assert sent[0]["status"] == 403
    assert calls["app"] == 0


def test_every_http_response_gets_anti_framing_headers():
    sent: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = BrowserSecurityMiddleware(
        app, settings=SimpleNamespace(api_token="", cors_origins="", effective_bool=lambda _: True)
    )
    asyncio.run(middleware(_scope([], method="GET", path="/"), receive, send))
    headers = dict(sent[0]["headers"])
    assert headers[b"content-security-policy"] == b"frame-ancestors 'none'"
    assert headers[b"x-frame-options"] == b"DENY"

    sent.clear()
    asyncio.run(middleware(
        _scope([(b"origin", b"https://evil.example")], method="GET", path="/api/stats"),
        receive,
        send,
    ))
    error_headers = dict(sent[0]["headers"])
    assert sent[0]["status"] == 403
    assert error_headers[b"content-security-policy"] == b"frame-ancestors 'none'"
    assert error_headers[b"x-frame-options"] == b"DENY"
