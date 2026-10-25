"""scripts/serve.py: one recorded instance, stale-record recovery, graceful stop.

The integration test runs a real server process against a throwaway output
directory and state directory, so it never touches a developer's install.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from scripts import serve

ROOT = Path(__file__).resolve().parents[1]
_GONE_PID = 2**22 + 12345   # above every default pid_max


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "STATE", tmp_path)
    monkeypatch.setattr(serve, "RECORD", tmp_path / "server.json")
    monkeypatch.setattr(serve, "STOP", tmp_path / "server.stop")
    return tmp_path


def _write(path: Path, **record):
    path.write_text(json.dumps({"host": "127.0.0.1", "port": 1, **record}))


def test_a_record_of_this_live_process_is_live(state):
    image = serve.process(os.getpid())[1] or sys.executable
    _write(state / "server.json", pid=os.getpid(), executable=image)
    assert serve.live_record() is not None


def test_a_record_whose_process_is_gone_is_stale(state):
    _write(state / "server.json", pid=_GONE_PID, executable=sys.executable)
    assert serve.live_record() is None


def test_a_reused_pid_running_another_program_is_stale(state):
    if not serve.process(os.getpid())[1]:
        pytest.skip("this platform cannot read a process image")
    _write(state / "server.json", pid=os.getpid(), executable=str(state / "other.exe"))
    assert serve.live_record() is None


def test_stop_with_a_stale_record_removes_it_and_kills_nothing(state):
    _write(state / "server.json", pid=_GONE_PID, executable=sys.executable)
    assert serve.main(["stop"]) == 0
    assert not (state / "server.json").exists()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_start_refuses_a_second_instance_and_stop_is_graceful(tmp_path):
    port = _free_port()
    env = {**os.environ, "WB_SERVER_STATE_DIR": str(tmp_path / "state"),
           "OUTPUT_DIR": str(tmp_path / "output"), "API_TOKEN": "", "WARM_UP": "false",
           "PROBE_DEVICE": "false", "ENRICH_CAPTIONS": "false", "REMOTE_GPU_BASE_URL": "",
           "REMOTE_GPU_PROVISIONER": "manual", "LOG_LEVEL": "WARNING"}
    script = str(ROOT / "scripts" / "serve.py")
    server = subprocess.Popen([sys.executable, script, "start", "--port", str(port)],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/system",
                                            timeout=2) as response:
                    assert response.status == 200
                    break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(server.stdout.read() if server.stdout else "") from None
                time.sleep(0.3)
        second = subprocess.run([sys.executable, script, "start", "--port", str(_free_port())],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert second.returncode == 3 and "already running" in second.stderr
        stopped = subprocess.run([sys.executable, script, "stop", "--timeout", "30"],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "stopped" in stopped.stdout and "terminating" not in stopped.stderr
        assert server.wait(timeout=10) == 0
        assert not (tmp_path / "state" / "server.json").exists()
    finally:
        if server.poll() is None:
            server.kill()
