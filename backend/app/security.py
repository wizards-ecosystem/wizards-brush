"""Small, dependency-light request-boundary checks for the local web app."""
from __future__ import annotations

import json
import secrets
from http.cookies import CookieError, SimpleCookie
from urllib.parse import unquote, urlsplit

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 128 * 1024 * 1024
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FRAME_HEADERS = {
    "content-security-policy": "frame-ancestors 'none'",
    "x-frame-options": "DENY",
}


class _BodyTooLarge(Exception):
    pass


def allowed_origins(configured: str) -> list[str]:
    """Exact browser origins allowed in addition to the same served origin.

    A wildcard is intentionally ignored. This app can run without a token, so a
    wildcard would turn every web page into a caller of the local API.
    """
    extras = [value.strip().rstrip("/") for value in configured.split(",")]
    dev = [
        f"http://{host}:{port}"
        for host in ("localhost", "127.0.0.1")
        for port in ("5173", "4173")
    ]
    return list(dict.fromkeys([*dev, *(value for value in extras if value and value != "*")]))


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _hostname(host: str) -> str | None:
    if not host or len(host) > 255 or any(char in host for char in "\r\n/@\\"):
        return None
    try:
        return urlsplit(f"//{host}").hostname
    except ValueError:
        return None


def _same_origin(scope: Scope, host: str) -> str:
    scheme = str(scope.get("scheme") or "http")
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    return f"{scheme}://{host}".rstrip("/").lower()


def _cookie(headers: dict[str, str], name: str) -> str:
    try:
        parsed = SimpleCookie()
        parsed.load(headers.get("cookie", ""))
        return unquote(parsed[name].value) if name in parsed else ""
    except CookieError:  # malformed cookies are simply unauthenticated
        return ""


class BrowserSecurityMiddleware:
    """Enforce the trust boundary before routing or multipart parsing."""

    def __init__(self, app: ASGIApp, *, settings, max_body_bytes: int = MAX_REQUEST_BYTES):
        self.app = app
        self.settings = settings
        self.max_body_bytes = max_body_bytes

    async def _http_error(self, send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _close_ws(self, receive: Receive, send: Send, code: int) -> None:
        message = await receive()
        if message["type"] == "websocket.connect":
            await send({"type": "websocket.close", "code": code})

    def _origin_allowed(self, scope: Scope, headers: dict[str, str]) -> bool:
        origin = headers.get("origin", "").rstrip("/").lower()
        if not origin or origin == "null":
            return False
        host = headers.get("host", "")
        if origin == _same_origin(scope, host):
            return True
        return origin in {item.lower() for item in allowed_origins(self.settings.cors_origins)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        # Apply anti-framing policy at the outermost response boundary so the
        # SPA, API, generated files, and our own early error responses cannot
        # drift apart.  Vite applies the same policy to its separate dev origin.
        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for key, value in _FRAME_HEADERS.items():
                    response_headers[key] = value
            await send(message)

        response_send = secure_send if scope_type == "http" else send

        headers = _headers(scope)
        host = headers.get("host", "")
        hostname = _hostname(host)
        if hostname is None or (not self.settings.api_token and hostname.lower() not in _LOOPBACK_HOSTS):
            if scope_type == "websocket":
                await self._close_ws(receive, send, 4403)
            else:
                await self._http_error(response_send, 421, "untrusted Host header")
            return

        path = str(scope.get("path") or "")
        if scope_type == "websocket":
            if path.startswith("/api") and not self._origin_allowed(scope, headers):
                await self._close_ws(receive, send, 4403)
                return
            if (path.startswith("/api") and self.settings.api_token
                    and not secrets.compare_digest(
                        _cookie(headers, "wb_ws_token"), self.settings.api_token
                    )):
                await self._close_ws(receive, send, 4401)
                return
            await self.app(scope, receive, send)
            return

        origin = headers.get("origin")
        if path.startswith("/api"):
            # CORS controls whether a hostile page may read a response; it does
            # not stop the browser from issuing a costly GET.  Fetch Metadata
            # covers no-CORS element/navigation requests, while Origin covers
            # ordinary fetches.  Origin-less CLI clients remain supported.
            if headers.get("sec-fetch-site", "").lower() == "cross-site":
                await self._http_error(response_send, 403, "cross-site request rejected")
                return
            if origin is not None and not self._origin_allowed(scope, headers):
                await self._http_error(response_send, 403, "cross-origin request rejected")
                return

        if (path.startswith("/files/videos/") and path.endswith(".json")
                and not self.settings.effective_bool("embed_metadata")):
            await self._http_error(response_send, 404, "not found")
            return

        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_body_bytes:
                    await self._http_error(response_send, 413, "request body too large")
                    return
            except ValueError:
                await self._http_error(response_send, 400, "invalid Content-Length")
                return

        consumed = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_body_bytes:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await response_send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            if not response_started:
                await self._http_error(response_send, 413, "request body too large")
