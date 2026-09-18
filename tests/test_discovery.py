"""The API describes itself to programs and AI agents.

A client that knows only the base URL must be able to learn everything else:
how to authenticate, every route and schema, and how to use them. That
self-description is public and data-free; what it describes stays guarded.
"""
from __future__ import annotations

import pytest

from backend.app.config import settings
from backend.app.routers import job_api, tools

TOKEN = "discovery-test-token-0123456789abcdef"


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    return TOKEN


def test_discovery_is_public_while_the_api_stays_guarded(client, token):
    for path in ("/llms.txt", "/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/jobs/kinds").status_code == 401
    ok = client.get("/api/jobs/kinds", headers={"X-API-Token": token})
    assert ok.status_code == 200


def test_openapi_declares_the_token_and_this_server(client):
    doc = client.get("/openapi.json").json()
    scheme = doc["components"]["securitySchemes"]["ApiToken"]
    assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", "X-API-Token")
    assert doc["security"] == [{"ApiToken": []}]
    assert doc["servers"][0]["url"] == "http://localhost"
    assert "/api/jobs" in doc["paths"] and "/llms.txt" not in doc["paths"]


def test_the_server_address_follows_the_request(client, token):
    """Behind a tunnel the document must name the address the client used."""
    doc = client.get("/openapi.json", headers={"Host": "quiet-river.trycloudflare.com"}).json()
    assert doc["servers"][0]["url"] == "http://quiet-river.trycloudflare.com"
    guide = client.get("/llms.txt", headers={"Host": "quiet-river.trycloudflare.com"}).text
    assert "Base URL: http://quiet-river.trycloudflare.com" in guide


def test_llms_txt_is_a_complete_first_read(client):
    r = client.get("/llms.txt")
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["cache-control"] in ("no-store", "no-cache")
    text = r.text
    assert text.startswith("# The Wizard's Brush")
    for needle in ("Base URL: http://localhost", "X-API-Token", "http://localhost/openapi.json",
                   "http://localhost/api/jobs/kinds", "/wait?timeout=30", "request_id"):
        assert needle in text, needle
    # The full guide follows, with every kind named and no link left relative.
    assert "## Variant Sets" in text
    for kind in [*job_api.GENERATOR_INPUTS, *tools.TOOLS]:
        assert f"`{kind}`" in text, kind
    assert "](../" not in text and "](network-security.md)" not in text
    assert "blob/v" in text and "/scripts/api_example.py)" in text


def test_the_explorers_read_the_augmented_document(client):
    for path in ("/docs", "/redoc"):
        assert "/openapi.json" in client.get(path).text, path
