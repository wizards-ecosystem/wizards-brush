"""Keep files that hold credentials readable by their owner only.

`.env` and the runtime settings file carry the API token, the Remote GPU
secret and provider keys. On POSIX that is mode 0600. Windows has no mode
bits; the equivalent is an access list with inheritance removed and one grant,
to the current user. That is set with `icacls`, which ships with every Windows
since Vista and needs no administrator rights on a file the user owns.

Standard library only. Every function degrades: an ACL the system will not
let us change is reported, never fatal to reading settings.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


def _oem_text(raw: bytes | None) -> str:
    """Decode console-tool output. `icacls` writes in the OEM code page (cp850,
    cp437...), not UTF-8, so a path with an accent would not decode under
    PYTHONUTF8; an undecodable byte is replaced rather than raised."""
    if not raw:
        return ""
    codepage = 0
    if os.name == "nt":
        try:
            import ctypes

            codepage = int(ctypes.windll.kernel32.GetOEMCP())  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            codepage = 0
    try:
        return raw.decode(f"cp{codepage}" if codepage else "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _icacls(*args: str) -> tuple[int, str]:
    done = subprocess.run(["icacls", *args], capture_output=True, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return done.returncode, _oem_text(done.stdout) + _oem_text(done.stderr)


def _windows_principal() -> str:
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain and user else user


def restrict(path: Path) -> None:
    """Owner-only access. Raises OSError when it cannot be applied."""
    if os.name == "posix":
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
        return
    if os.name == "nt":
        principal = _windows_principal()
        if not principal:
            raise OSError("cannot tell which Windows account owns this process")
        code, output = _icacls(str(path), "/inheritance:r", "/grant:r", f"{principal}:(F)")
        if code != 0:
            raise OSError(f"icacls could not restrict {path.name}: {output.strip()[:300]}")


def is_private(path: Path) -> bool | None:
    """True when only the owner can read `path`; None when that cannot be told."""
    if os.name == "posix":
        return not stat.S_IMODE(path.stat().st_mode) & 0o077
    if os.name != "nt":
        return None
    try:
        code, output = _icacls(str(path))
    except OSError:
        return None
    if code != 0:
        return None
    return grants_only(output, str(path), _windows_principal())


def grants_only(listing: str, path: str, principal: str) -> bool:
    """Whether an `icacls` listing grants access to `principal` and no one else.

    The first line starts with the path; each grant is "<account>:(<rights>)".
    The trailing status line ("Successfully processed ...") is not a grant.
    """
    grants: list[str] = []
    for raw in listing.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("successfully processed"):
            continue
        if line.startswith(path):
            line = line[len(path):].strip()
        account, sep, _rights = line.rpartition(":(")
        if sep and account:
            grants.append(account.split(":")[0].strip())
    wanted = principal.lower()
    short = wanted.split("\\")[-1]
    return bool(grants) and all(g.lower() in {wanted, short} for g in grants)
