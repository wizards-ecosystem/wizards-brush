"""Host RAM, portably, with an honest unknown.

Linux reads /proc/meminfo; Windows asks GlobalMemoryStatusEx through ctypes.
Both are the operating system's own estimate of memory a new allocation can get
without paging: MemAvailable on Linux (reclaimable page cache counted as free)
and ullAvailPhys on Windows (free plus standby pages). Anywhere else, or when
the call fails, the answer is unknown and says why. A probe never raises and
never guesses: callers decide what an unknown means, and a false number is
worse than none.

Standard library only, so the registry and the test suite can import it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HostMemory:
    available_bytes: int | None
    total_bytes: int | None
    source: str                  # "proc_meminfo", "GlobalMemoryStatusEx", or "unknown"
    error: str = ""              # why the figures are unknown, when they are

    @property
    def known(self) -> bool:
        return self.available_bytes is not None

    def as_dict(self) -> dict[str, Any]:
        return {"available_bytes": self.available_bytes, "total_bytes": self.total_bytes,
                "source": self.source, "error": self.error}


def parse_meminfo(text: str) -> HostMemory:
    """MemAvailable and MemTotal from /proc/meminfo text (values are in kB)."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name in {"MemAvailable", "MemTotal"} and parts and parts[0].isdigit():
            values[name] = int(parts[0]) * 1024
    if "MemAvailable" not in values:
        # Kernels before 3.14 lack MemAvailable. MemFree would read as almost
        # nothing on a warm machine, so it is not a substitute.
        return HostMemory(None, values.get("MemTotal"), "proc_meminfo",
                          "MemAvailable is not reported by this kernel")
    return HostMemory(values["MemAvailable"], values.get("MemTotal"), "proc_meminfo")


def _linux(meminfo: Path) -> HostMemory:
    try:
        return parse_meminfo(meminfo.read_text())
    except OSError as exc:
        return HostMemory(None, None, "proc_meminfo", f"cannot read {meminfo}: {exc}")


def _windows(kernel32: Any = None) -> HostMemory:
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        api = kernel32 if kernel32 is not None else ctypes.windll.kernel32  # type: ignore[attr-defined]
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not api.GlobalMemoryStatusEx(ctypes.byref(status)):
            return HostMemory(None, None, "GlobalMemoryStatusEx", "GlobalMemoryStatusEx failed")
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return HostMemory(None, None, "GlobalMemoryStatusEx", f"probe failed: {exc}")
    return HostMemory(int(status.ullAvailPhys), int(status.ullTotalPhys), "GlobalMemoryStatusEx")


def probe(platform: str | None = None, *, meminfo: Path = Path("/proc/meminfo"),
          kernel32: Any = None) -> HostMemory:
    """Host memory now. `platform` and the sources are injectable for tests."""
    name = platform or sys.platform
    try:
        if name.startswith("linux"):
            return _linux(meminfo)
        if name == "win32":
            return _windows(kernel32)
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return HostMemory(None, None, "unknown", f"probe failed: {exc}")
    return HostMemory(None, None, "unknown", f"no host memory probe for {name}")
