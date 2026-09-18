"""Keep this machine's filesystem layout out of what the API returns.

Job params record absolute paths to their input files, and a failed job's
error carries a traceback. A client never needs to know where the app is
installed or whose home directory it lives in, so API views show paths inside
the app folder relative to it - still enough to identify the file - and any
other path under the owner's home as `~/...`.

Views only: stored rows keep the real paths, because rerun, retry and restart
recovery read the database, never these responses.
"""
from __future__ import annotations

import os
from typing import Any

from .config import ROOT


def _owner_home() -> str:
    """The account's real home directory. The app runs with HOME redirected into
    the project (scripts/project-env.sh), so the environment cannot answer."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return os.path.expanduser("~")


# Longest first, so the app folder is made relative before the home it sits in
# is shortened to `~`.
_ROOTS = sorted({str(ROOT), os.path.realpath(ROOT)}, key=len, reverse=True)
_HOME = _owner_home().rstrip(os.sep)


def redact_text(text: str) -> str:
    for root in _ROOTS:
        text = text.replace(root + os.sep, "").replace(root, ".")
    if len(_HOME) > 1:
        text = text.replace(_HOME + os.sep, "~" + os.sep).replace(_HOME, "~")
    return text


def redact(value: Any) -> Any:
    """`value` with local paths redacted, through nested dicts and lists."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value
