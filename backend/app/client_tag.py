"""Which client submitted a job: the UI, MCP, the OpenAI image API, or the API token's name.

Stored on the job row at creation and copied into output metadata. It is
provenance, not an authorization check — a caller can still claim to be a
browser by sending a browser Origin.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from starlette.requests import Request

from .config import settings
from .security import _same_origin, allowed_origins

_current: ContextVar[str | None] = ContextVar("brush_client", default=None)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


def current() -> str:
    """The client label for a job created on this task. Defaults to ``api``."""
    value = _current.get()
    return value or "api"


@contextmanager
def use(name: str) -> Iterator[None]:
    """Pin the label for jobs created inside the block. MCP uses ``mcp``."""
    token = _current.set(_clean(name) or "api")
    try:
        yield
    finally:
        _current.reset(token)


def bind_request(request: Request) -> Token[str | None]:
    """Set the label from this HTTP request. The caller resets the token."""
    return _current.set(_from_request(request))


def reset(token: Token[str | None]) -> None:
    _current.reset(token)


def token_name() -> str:
    """The configured name of the single API token, or ``api`` when unnamed."""
    return _clean(settings.api_token_name) or "api"


def _clean(raw: str) -> str:
    text = raw.strip()
    return text if _NAME.fullmatch(text) else ""


def _browser(request: Request) -> bool:
    origin = request.headers.get("origin", "").rstrip("/").lower()
    if origin and origin != "null":
        host = request.headers.get("host", "")
        scope = request.scope
        if origin == _same_origin(scope, host):
            return True
        allowed = {item.lower() for item in allowed_origins(settings.cors_origins)}
        if origin in allowed:
            return True
    site = request.headers.get("sec-fetch-site", "").lower()
    return site in {"same-origin", "same-site", "none"}


def _token_presented(request: Request) -> bool:
    if request.headers.get("x-api-token") is not None:
        return True
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    return scheme.lower() == "bearer" and bool(value.strip())


def _from_request(request: Request) -> str:
    browser = _browser(request)
    if browser and not _token_presented(request):
        return "ui"
    if _token_presented(request):
        return token_name()
    if browser:
        return "ui"
    return "api"
