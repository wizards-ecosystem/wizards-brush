"""The project version, from one place.

It was declared three times — `pyproject.toml`, `frontend/package.json`, and a
string literal in the FastAPI constructor — and reported nowhere a user could
see it. Three copies of a number that must agree is three chances to ship a
build that misreports itself, and a version nobody can read is a bug report that
starts with "which version?" and stops there.

`pyproject.toml` is the source. Two ways to reach it, because both situations are
real:

* **Installed as a distribution** (a wheel, a `pip install .`): the packaging
  metadata is authoritative and `pyproject.toml` may not be beside us at all.
* **Run from a checkout**, which is how this project is normally used: there is
  no distribution metadata, but `pyproject.toml` is right there.

Falls back to "0.0.0+unknown" rather than raising. Refusing to start because the
version string could not be determined would be an absurd trade.
"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

UNKNOWN = "0.0.0+unknown"

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


@lru_cache(maxsize=1)
def get_version() -> str:
    """The running version. Cached: it cannot change while the process lives."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("wizards-brush")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib
        pass
    try:
        data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return UNKNOWN
