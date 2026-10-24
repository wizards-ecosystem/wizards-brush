"""Run, stop and inspect the app server with a recorded, verifiable identity.

    python scripts/serve.py start [--reload] [--host H] [--port P]
    python scripts/serve.py stop [--timeout S]
    python scripts/serve.py status

The Windows launchers (scripts/*.ps1, wizards-brush.cmd) use this; it works on
Linux too, where scripts/start.sh remains the documented path.

* **One instance.** `start` records `.runtime/state/server.json` (pid,
  executable, host, port). A record whose process is gone, or is some other
  program that reused the pid, is stale and is replaced: that is restart
  recovery after a crash or a reboot. A live record refuses a second start.
  The database lock (db.acquire_process_lock) remains the real guard.
* **Graceful stop.** `stop` writes `.runtime/state/server.stop`; the server
  notices within half a second and shuts down through the app's lifespan, so
  a rented GPU is released and the database lock is let go. Only when it has
  not exited after `--timeout` is that one recorded process terminated, after
  checking it is still the executable that was recorded. Nothing is ever
  killed by name.
* **No orphans.** On Windows the server puts itself in a job object that kills
  every process still in it when the server's last handle closes, so an
  ffmpeg or a preflight subprocess cannot outlive a crashed or force-stopped
  server. On POSIX a stop signals the server's own process group.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Overridable so tests never touch a real install's record.
STATE = Path(os.environ.get("WB_SERVER_STATE_DIR") or ROOT / ".runtime" / "state")
RECORD = STATE / "server.json"
STOP = STATE / "server.stop"
_JOB_HANDLE: Any = None          # kept alive for the life of the process


# ---- process identity ---------------------------------------------------------------
def _windows_process(pid: int) -> tuple[bool, str]:
    """(alive, image path) for a Windows pid, through the Win32 API."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    query_limited = 0x1000
    handle = kernel32.OpenProcess(query_limited, False, pid)
    if not handle:
        return False, ""
    try:
        code = wintypes.DWORD()
        alive = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        image = buffer.value if kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)) else ""
        return alive, image
    finally:
        kernel32.CloseHandle(handle)


def process(pid: int) -> tuple[bool, str]:
    """Whether `pid` is running, and its executable when that can be read."""
    if pid <= 0:
        return False, ""
    if sys.platform == "win32":
        return _windows_process(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, ""
    except PermissionError:
        return True, ""
    try:
        return True, os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return True, ""


def _same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def read_record() -> dict[str, Any] | None:
    try:
        data = json.loads(RECORD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def live_record() -> dict[str, Any] | None:
    """The recorded server when it is still that process; None when stale."""
    record = read_record()
    if record is None:
        return None
    alive, image = process(int(record.get("pid") or 0))
    if not alive:
        return None
    # A reused pid belongs to whatever now holds it. An unreadable image (a
    # process of another account) is not proof either way; treat it as ours
    # only when the record says so and the platform could not tell.
    if image and not _same_file(image, str(record.get("executable") or "")):
        return None
    return record


# ---- start -----------------------------------------------------------------------------
def _contain_children() -> None:
    """Windows: every child this server starts dies with it."""
    global _JOB_HANDLE
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        print("warning: could not create a job object; children may outlive a crash",
              file=sys.stderr)
        return
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
    extended_info = 9                                         # JobObjectExtendedLimitInformation
    ok = kernel32.SetInformationJobObject(job, extended_info, ctypes.byref(limits),
                                          ctypes.sizeof(limits))
    ok = ok and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    if not ok:
        print("warning: could not join a job object; children may outlive a crash",
              file=sys.stderr)
        kernel32.CloseHandle(job)
        return
    _JOB_HANDLE = job


def _settings_address(host: str | None, port: int | None) -> tuple[str, int, bool]:
    sys.path.insert(0, str(ROOT))
    from backend.app.config import settings

    return host or settings.host, int(port or settings.port), bool(settings.api_token)


def start(args: argparse.Namespace) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    running = live_record()
    if running is not None:
        print(f"already running (pid {running['pid']}) on "
              f"http://{running['host']}:{running['port']}", file=sys.stderr)
        return 3
    if RECORD.exists():
        print("note: replacing a stale server record from a previous run")
    STOP.unlink(missing_ok=True)
    host, port, token = _settings_address(args.host, args.port)
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        print(f"refusing non-loopback HOST={host} without API_TOKEN", file=sys.stderr)
        return 2
    _contain_children()
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.setpgid(0, 0)        # a stop signals this group, children included
    # The image actually running, not sys.executable: in a Windows venv that
    # names the Scripts\python.exe launcher, while this process is the base
    # interpreter it started.
    image = process(os.getpid())[1] or sys.executable
    record = {"pid": os.getpid(), "executable": image, "host": host, "port": port,
              "root": str(ROOT), "started_at": time.time(), "reload": bool(args.reload)}
    temporary = RECORD.with_suffix(".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, RECORD)
    print(f"==> Serving on http://{host}:{port}", flush=True)
    try:
        return _serve(host, port, reload=args.reload)
    finally:
        if (read_record() or {}).get("pid") == os.getpid():
            RECORD.unlink(missing_ok=True)
        STOP.unlink(missing_ok=True)


def _serve(host: str, port: int, *, reload: bool) -> int:
    import uvicorn

    if reload:
        # The reloader runs the app in a child; the job object (Windows) or the
        # process group (POSIX) takes both down together.
        uvicorn.run("backend.app.main:app", host=host, port=port, reload=True,
                    reload_dirs=[str(ROOT / "backend")])
        return 0
    server = uvicorn.Server(uvicorn.Config("backend.app.main:app", host=host, port=port))

    def watch() -> None:
        while not server.should_exit:
            if STOP.exists():
                print("==> Stop requested; shutting down", flush=True)
                server.should_exit = True
                return
            time.sleep(0.5)

    threading.Thread(target=watch, daemon=True).start()
    server.run()
    return 0


# ---- stop ------------------------------------------------------------------------------
def _terminate(pid: int) -> None:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x0001, False, pid)              # PROCESS_TERMINATE
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def stop(args: argparse.Namespace) -> int:
    record = live_record()
    if record is None:
        if RECORD.exists():
            RECORD.unlink(missing_ok=True)
            print("not running (removed a stale record)")
        else:
            print("not running")
        return 0
    pid = int(record["pid"])
    STATE.mkdir(parents=True, exist_ok=True)
    STOP.write_text(str(pid), encoding="utf-8")
    if record.get("reload"):
        # The development reloader does not watch the stop file. POSIX asks its
        # group politely; Windows has no such signal for a hidden console, so
        # the reloader is ended and its job object takes the worker with it.
        if sys.platform == "win32":
            _terminate(pid)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if live_record() is None:
            print(f"stopped (pid {pid})")
            STOP.unlink(missing_ok=True)
            return 0
        time.sleep(0.25)
    # Still the same recorded executable? Only then is it ours to end.
    current = live_record()
    if current is not None and int(current["pid"]) == pid:
        print(f"pid {pid} did not stop within {args.timeout:.0f}s; terminating it",
              file=sys.stderr)
        _terminate(pid)
        time.sleep(1.0)
    RECORD.unlink(missing_ok=True)
    STOP.unlink(missing_ok=True)
    return 0 if process(pid)[0] is False else 1


def status(_args: argparse.Namespace) -> int:
    record = live_record()
    if record is None:
        print("not running")
        return 1
    print(f"running (pid {record['pid']}) on http://{record['host']}:{record['port']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("start")
    begin.add_argument("--host")
    begin.add_argument("--port", type=int)
    begin.add_argument("--reload", action="store_true")
    end = sub.add_parser("stop")
    end.add_argument("--timeout", type=float, default=30.0)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    return {"start": start, "stop": stop, "status": status}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
