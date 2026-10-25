"""Portable host-RAM probe: real figures where the OS gives them, and an
honest unknown, with a reason, everywhere else."""
from __future__ import annotations

import ctypes

from backend.app.generators import vram
from backend.app.utils import hostmem

_MEMINFO = "MemTotal:       32000000 kB\nMemFree:          100000 kB\nMemAvailable:   20000000 kB\n"


def test_linux_reads_mem_available_not_mem_free(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text(_MEMINFO)
    memory = hostmem.probe("linux", meminfo=path)
    assert memory.available_bytes == 20000000 * 1024
    assert memory.total_bytes == 32000000 * 1024
    assert memory.known and memory.error == ""


def test_an_old_kernel_without_mem_available_is_unknown_not_mem_free():
    memory = hostmem.parse_meminfo("MemTotal: 1000 kB\nMemFree: 900 kB\n")
    assert memory.available_bytes is None and not memory.known
    assert "MemAvailable" in memory.error


def test_an_unreadable_meminfo_is_unknown(tmp_path):
    memory = hostmem.probe("linux", meminfo=tmp_path / "missing")
    assert memory.available_bytes is None and "cannot read" in memory.error


class _FakeKernel32:
    def __init__(self, ok: bool, avail: int = 0, total: int = 0) -> None:
        self.ok, self.avail, self.total = ok, avail, total

    def GlobalMemoryStatusEx(self, ref):
        status = ref._obj
        assert status.dwLength == ctypes.sizeof(status)
        status.ullAvailPhys, status.ullTotalPhys = self.avail, self.total
        return 1 if self.ok else 0


def test_windows_reports_available_physical_memory():
    memory = hostmem.probe("win32", kernel32=_FakeKernel32(True, 12 * 1024 ** 3, 32 * 1024 ** 3))
    assert memory.available_bytes == 12 * 1024 ** 3
    assert memory.total_bytes == 32 * 1024 ** 3
    assert memory.source == "GlobalMemoryStatusEx"


def test_a_failed_windows_call_is_unknown():
    memory = hostmem.probe("win32", kernel32=_FakeKernel32(False))
    assert memory.available_bytes is None and "failed" in memory.error


def test_a_windows_probe_that_raises_is_unknown():
    class Broken:
        def GlobalMemoryStatusEx(self, ref):
            raise OSError("access denied")

    memory = hostmem.probe("win32", kernel32=Broken())
    assert memory.available_bytes is None and "access denied" in memory.error


def test_an_unsupported_platform_is_unknown_with_a_reason():
    memory = hostmem.probe("darwin")
    assert memory.available_bytes is None and "darwin" in memory.error


def test_the_load_guard_reads_the_portable_probe(monkeypatch):
    monkeypatch.setattr(hostmem, "probe", lambda: hostmem.HostMemory(None, None, "unknown", "x"))
    assert vram.free_host_ram_bytes() is None
    monkeypatch.setattr(hostmem, "probe", lambda: hostmem.HostMemory(5, 10, "proc_meminfo"))
    assert vram.free_host_ram_bytes() == 5


def test_this_machine_answers_or_says_why():
    memory = hostmem.probe()
    assert memory.known or memory.error
