"""Test bootstrap: point the app at a throwaway output dir BEFORE any backend
import, so the module-level Settings/engine bind to the temp DB, not ./output."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TEST_TEMP_ROOT = _ROOT / ".runtime" / "tmp" / "tests"
_TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
_SESSION_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="pytest-session-", dir=_TEST_TEMP_ROOT))
os.environ["TMPDIR"] = str(_SESSION_TEMP_ROOT)
os.environ["TMP"] = str(_SESSION_TEMP_ROOT)
os.environ["TEMP"] = str(_SESSION_TEMP_ROOT)
tempfile.tempdir = str(_SESSION_TEMP_ROOT)
_OUTPUT_ROOT = _SESSION_TEMP_ROOT / "output"
os.environ["OUTPUT_DIR"] = str(_OUTPUT_ROOT)
# The LoRA library is a REAL user directory holding multi-gigabyte downloads,
# and LORA_DIR is read from the developer's own .env. test_loras_catalog writes
# fixtures into settings.loras_dir and unlinks them on cleanup, so inheriting
# that setting meant `make test` silently deleted whatever the developer had
# installed. Redirected here, before any backend import, for the same reason
# OUTPUT_DIR is: a test must never be able to reach a real library.
os.environ["LORA_DIR"] = str(_SESSION_TEMP_ROOT / "loras")
os.environ["WARM_UP"] = "false"
# The startup device probe imports torch to read the GPU's name and VRAM. That
# is correct in production and fatal here: the heavy-import guard below is what
# proves the torch-free CI job is safe.
os.environ["PROBE_DEVICE"] = "false"
# Same reason, and the same class of bug: resolving the nunchaku backend imports
# nunchaku (hence torch) to ask whether its kernels are usable. Pinned rather
# than inherited so the suite's behaviour does not depend on which backend the
# developer happens to have in their own .env — a machine set to LOCAL_QUANT=
# nunchaku would otherwise fail the heavy-import guard on an unrelated change.
os.environ["LOCAL_QUANT"] = "4bit"
# Tests that exercise model switching need a deterministic second slot. Public
# defaults deliberately ship only the starter model, while a developer's .env
# often configures this slot; pin it here so CI and local runs see one contract.
os.environ["LOCAL_IMAGE_MODEL_HQ"] = "Tongyi-MAI/Z-Image"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["REMOTE_GPU_BASE_URL"] = ""  # env beats .env — keep tests off any real tunnel
# Persisting an asset calls enrichment.enqueue(), which spawns a daemon thread
# that imports transformers to load Florence-2. Off by default in tests: it is
# a heavy import on a background thread, so it lands in sys.modules at a
# nondeterministic point and trips the guard below from an unrelated test.
os.environ["ENRICH_CAPTIONS"] = "false"
os.environ.pop("API_TOKEN", None)
# Never authenticate against a developer's real tunnel from the test suite.
os.environ.pop("REMOTE_GPU_SHARED_SECRET", None)

import pytest


def pytest_sessionfinish(session, exitstatus):
    """The test database and temporary files are project-local and ephemeral."""
    shutil.rmtree(_SESSION_TEMP_ROOT, ignore_errors=True)

# The heavy stack that must only ever be imported inside functions. CI installs
# none of these, so a module-level import would crash collection there — this
# guard makes the same mistake fail loudly in a full dev env too.
_HEAVY = ("torch", "diffusers", "transformers", "imageio", "numpy", "cv2", "timm")


def _heavy_loaded() -> list[str]:
    return [m for m in _HEAVY if m in sys.modules]


@pytest.fixture(autouse=True)
def _no_heavy_imports_per_test(request):
    """Enforce the lazy-import convention: no test may pull in torch (or the rest
    of the heavy diffusion/imaging stack). This is what makes the torch-free CI
    job safe.

    Checked per test rather than once at session teardown so the failure names
    the test that did it. A background thread can still land an import during a
    later test, so this reports the first test where it became visible, not
    necessarily the one that started it — the session-scoped backstop below
    catches anything that slips in after the last test finishes."""
    before = set(_heavy_loaded())
    yield
    new = [m for m in _heavy_loaded() if m not in before]
    assert not new, (
        f"{request.node.nodeid} triggered heavy imports: {new}. "
        "Move the import inside the function that needs it."
    )


@pytest.fixture(autouse=True, scope="session")
def _no_heavy_imports_session():
    yield
    loaded = _heavy_loaded()
    assert not loaded, f"the test session triggered heavy imports: {loaded}"


async def _async_noop(*a, **kw):
    return None


@pytest.fixture()
def no_queue(monkeypatch):
    """Stub the job-queue submit so generate endpoints create DB rows but never
    execute handlers (which would import torch / hit the network)."""
    import backend.app.routers.common as common

    monkeypatch.setattr(common, "enqueue", _async_noop)
    return


@pytest.fixture(scope="module")
def client():
    """TestClient with lifespan (init_db + lanes). Module-scoped so per-module
    reconcile_orphans doesn't cancel jobs created by earlier test files."""
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app, base_url="http://localhost") as c:
        yield c


@pytest.fixture()
def remote_gpu_env():
    """Point remote_gpu_client at a fake URL/secret via the runtime-overrides file."""
    from backend.app.config import settings

    prev = settings.load_overrides()
    settings.save_overrides({"remote_gpu_base_url": "http://colab.test",
                             "remote_gpu_shared_secret": "s3cr3t-xyz"})
    yield {"url": "http://colab.test", "secret": "s3cr3t-xyz"}
    settings.save_overrides(prev)
