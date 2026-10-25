"""Public, self-describing entry points for programs and AI agents.

Everything served here is documentation: no library data, no credentials, and
nothing under /api, so it answers without a token while everything it
describes stays guarded exactly as before.

- /openapi.json - the OpenAPI document, with the `X-API-Token` header declared
  as its security scheme and this server's own address as its `servers` entry.
  The address is computed per request (scheme and host as the client sees
  them, behind a proxy or tunnel too), because tools that import OpenAPI -
  API clients, agent frameworks, custom GPT actions - need an absolute server
  URL and a declared scheme to call anything.
- /docs, /redoc - the interactive explorers over that document; Authorize
  takes the token.
- /llms.txt - a plain-text guide for language models (the llms.txt
  convention): what the service is, how to authenticate, where to discover the
  rest and the core loop, followed by docs/api.md in full.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .config import ROOT
from .version import get_version

TOKEN_HEADER = "X-API-Token"
SCHEME = "ApiToken"
REPOSITORY = "https://github.com/wizards-ecosystem/wizards-brush"
# Documentation, not data - but it names this server's address, so no shared
# cache in front of it (a CDN, a tunnel) may hand one client's copy to another.
_NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(include_in_schema=False)


def base_url(request: Request) -> str:
    """This server's address as the client reached it."""
    return str(request.base_url).rstrip("/")


def openapi_document(app: FastAPI, request: Request) -> dict[str, Any]:
    """FastAPI's generated document plus the auth scheme and this server's URL."""
    generated = app.openapi()   # generated once and cached by FastAPI
    components = dict(generated.get("components") or {})
    components["securitySchemes"] = {
        **(components.get("securitySchemes") or {}),
        SCHEME: {
            "type": "apiKey",
            "in": "header",
            "name": TOKEN_HEADER,
            "description": "The server's API_TOKEN, sent on every /api request. Required "
                           "whenever the server sets one, as it must for any access "
                           "beyond this machine.",
        },
    }
    return {**generated, "components": components, "security": [{SCHEME: []}],
            "servers": [{"url": base_url(request), "description": "This server"}]}


# Links in docs/api.md are relative to the repository; outside it they must be
# absolute to resolve.
_RELATIVE_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


def _absolute_links(markdown: str, version: str) -> str:
    def repl(match: re.Match[str]) -> str:
        target = match.group(1)
        path = target[3:] if target.startswith("../") else f"docs/{target}"
        return f"]({REPOSITORY}/blob/v{version}/{path})"

    return _RELATIVE_LINK.sub(repl, markdown)


@lru_cache(maxsize=1)
def _guide() -> str:
    try:
        text = (ROOT / "docs" / "api.md").read_text(encoding="utf-8")
    except OSError:
        return f"The full guide is docs/api.md in {REPOSITORY}."
    return _absolute_links(text, get_version())


def llms_text(base: str) -> str:
    """The guide an agent should read first, addressed to this server."""
    return f"""# The Wizard's Brush

> A local-first image and video studio. Its HTTP API can do everything the web
> interface does: generate and edit images and video, run finishing tools, and
> run Variant Sets (one source, every combination of named axes, each variant
> tracked, validated and exportable).

Base URL: {base}

Authentication: send the header `{TOKEN_HEADER}: <token>` with every request
under `/api`. Without it the server answers 401. The token comes from the
server's operator. This page, the OpenAPI document and the explorers need no
token.

## Discover the API

- [OpenAPI document]({base}/openapi.json): every route with its request and
  response schemas; declares the `{TOKEN_HEADER}` scheme and this base URL.
- [Job kinds]({base}/api/jobs/kinds): every generator and tool that can run
  here right now, the inputs each takes, and a JSON Schema for its settings.
- [Variant Set capabilities]({base}/api/variant-sets/capabilities):
  operations, finishing processors, validation checks and limits.
- [Interactive explorer]({base}/docs): try requests after **Authorize**.

## Core loop

1. `POST {base}/api/assets/import` (multipart field `file`) returns an asset
   with an `id`. Outputs of earlier jobs are assets too: `GET {base}/api/assets`.
2. `POST {base}/api/jobs` with JSON `{{"kind": "...", "params": {{...}},
   "inputs": {{"images": [<asset id>]}}, "request_id": "<16-128 of A-Za-z0-9_->"}}`
   returns a `job_id`.
3. `GET {base}/api/jobs/<job_id>/wait?timeout=30`, repeated until `settled` is
   true, returns the job (`status`: done, error or canceled) and its output
   `assets`.
4. `GET {base}/api/assets/<asset id>/file` returns the file.

Rules that matter:

- Settings are validated strictly. An unknown name or an out-of-range value is
  a 400 whose `detail` names it: read it, fix the request, send it again.
- Resending a `request_id` returns the original work instead of queuing it
  twice, so a request that timed out can be retried safely.
- Work is asynchronous. Queue it, then wait in repeated calls; each returns
  within 60 seconds.
- Files are fetched from `/api/assets/<id>/file`. The `url` and `thumb_url`
  fields of an asset are for browser sessions only.

## Full guide

{_guide()}"""


@router.get("/openapi.json")
async def openapi_json(request: Request) -> JSONResponse:
    return JSONResponse(openapi_document(request.app, request), headers=_NO_STORE)


@router.get("/docs")
async def swagger_ui(request: Request) -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/openapi.json",
                               title=f"{request.app.title} - API explorer")


@router.get("/redoc")
async def redoc(request: Request) -> HTMLResponse:
    return get_redoc_html(openapi_url="/openapi.json", title=f"{request.app.title} - API")


@router.get("/llms.txt")
async def llms(request: Request) -> PlainTextResponse:
    return PlainTextResponse(llms_text(base_url(request)), headers=_NO_STORE)
