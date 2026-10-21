"""Structured refusals a caller, especially an agent, can correct.

A job request that is rejected still says what went wrong in `detail`, the
string every existing client already reads. Alongside it, when the server
knows them: `code` (stable), `field` (which input to change), `valid_options`,
and up to three short `suggestions`.

The idea of handing an agent those fields, and of checking a request without
queueing it, follows the behaviour described for comfy-mcp. That project is
AGPL-3.0; nothing here is taken from its source.
"""
from __future__ import annotations

import difflib
from typing import Any

from fastapi import HTTPException


class RequestError(HTTPException):
    """A refusal that names the field and the legal fix, when those are known."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str,
        field: str | None = None,
        valid_options: list[Any] | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.field = field
        self.valid_options = list(valid_options) if valid_options else None
        self.suggestions = [item for item in (suggestions or []) if item][:3]


def close_suggestions(value: object, options: list[Any]) -> list[str]:
    """Up to three 'did you mean' hints. Empty when nothing is close."""
    pool = [str(option) for option in options]
    matches = difflib.get_close_matches(str(value), pool, n=3, cutoff=0.6)
    return [f"Did you mean {match!r}?" for match in matches]


def error_body(exc: RequestError) -> dict[str, Any]:
    """The JSON body shared by the jobs API and MCP tool errors."""
    body: dict[str, Any] = {"detail": str(exc.detail), "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    if exc.valid_options:
        body["valid_options"] = exc.valid_options
    if exc.suggestions:
        body["suggestions"] = exc.suggestions
    return body


def as_request_error(exc: HTTPException) -> RequestError:
    """Give a plain HTTPException the same shape, preserving one already structured."""
    if isinstance(exc, RequestError):
        return exc
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code, field, options, suggestions = _classify(exc.status_code, detail)
    return RequestError(
        exc.status_code, detail, code=code, field=field,
        valid_options=options, suggestions=suggestions,
    )


def _classify(
    status: int, detail: str,
) -> tuple[str, str | None, list[Any] | None, list[str]]:
    text = detail.lower()
    if "request_id must be" in text:
        return (
            "invalid_request", "request_id", None,
            ["Use 16-128 letters, numbers, underscores, or hyphens."],
        )
    if status == 404 or "does not exist" in text or "not found" in text:
        return (
            "not_found", None, None,
            ["Check the id, or import the file and use the new asset id."],
        )
    if status == 503 and "remote gpu" in text:
        return (
            "remote_offline", "kind", None,
            ["Start the Remote GPU worker, then retry."],
        )
    if status == 503:
        return "unavailable", None, None, []
    if status == 409:
        return "conflict", None, None, []
    if "is not a setting" in text:
        return "unknown_setting", None, None, []
    if "must be between" in text:
        return "out_of_range", None, None, []
    if "unknown or unavailable kind" in text:
        return "kind_unavailable", "kind", None, []
    if status == 400:
        return "invalid_request", None, None, []
    return "error", None, None, []
