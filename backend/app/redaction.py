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
    """The account's real home directory. The app runs with HOME (and on
    Windows USERPROFILE) redirected into the project by project-env, so the
    environment cannot answer; ask the account database instead."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        pass
    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(260)
            # CSIDL_PROFILE: the profile folder from the registry, not from env.
            if ctypes.windll.shell32.SHGetFolderPathW(  # type: ignore[attr-defined]
                    None, 0x0028, None, 0, buffer) == 0 and buffer.value:
                return buffer.value
        except (AttributeError, OSError):
            pass
    return os.path.expanduser("~")


# Longest first, so the app folder is made relative before the home it sits in
# is shortened to `~`.
_ROOTS = sorted({str(ROOT), os.path.realpath(ROOT)}, key=len, reverse=True)
_HOME = _owner_home().rstrip("\\/")
# Windows accepts both separators, and a path built with "/" after a native
# root ("C:\\app" + "/output") must not slip past as "./output".
_SEPS = (os.sep, "/") if os.sep != "/" else ("/",)


def redact_text(text: str) -> str:
    for root in _ROOTS:
        for sep in _SEPS:
            text = text.replace(root + sep, "")
        text = text.replace(root, ".")
    if len(_HOME) > 1:
        for sep in _SEPS:
            text = text.replace(_HOME + sep, "~" + sep)
        text = text.replace(_HOME, "~")
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
