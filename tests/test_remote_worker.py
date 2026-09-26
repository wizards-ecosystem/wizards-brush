"""The remote worker package: its deployment contract and its hard edges.

`worker/` is the one part of this repo with no runtime coverage in CI - it needs
a GPU to do anything - so these tests pin the parts that have actually broken:
the ingress that runs before a body is parsed, the request bounds, the
dependency lock, and the host detection that decides how the worker is reached.

`worker.runtime` imports nothing heavy, so it is imported directly. `worker.api`
and `worker.pipelines` import torch at module scope, which conftest's _HEAVY
guard forbids, and `worker.config` raises at import without a real secret - so
functions from those three are lifted out of the source instead.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re
import stat

import pytest

from scripts import make_remote_gpu_config as gen
from worker import build_id, runtime

REPO_ROOT = gen.ROOT
WORKER = REPO_ROOT / "worker"
API_SRC = (WORKER / "api.py").read_text()
PIPELINES_SRC = (WORKER / "pipelines.py").read_text()
CONFIG_SRC = (WORKER / "config.py").read_text()
DOCKERFILE_SRC = (REPO_ROOT / "docker" / "remote-gpu" / "Dockerfile").read_text()


def _src(hf='HF_TOKEN = ""', secret='REMOTE_GPU_SHARED_SECRET = ""',
         cfg="REMOTE_GPU_CONFIG: dict = {}") -> str:
    return (
        f"# header\n{hf}\n{secret}\n{cfg}\n"
        'REMOTE_GPU_REQUIREMENTS_LOCK = ""\n\nimport os\nprint(os)\n'
    )


















def _remote_ingress_class():
    tree = ast.parse(API_SRC)
    wanted = {"_RemoteBodyTooLarge", "_RemoteIngressMiddleware"}
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "MAX_REMOTE_BODY_BYTES"
                    for target in node.targets))
        or (isinstance(node, ast.ClassDef) and node.name in wanted)
    ]
    import hmac

    scope: dict = {"hmac": hmac}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<remote-ingress>", "exec"), scope)
    return scope["_RemoteIngressMiddleware"]


def test_remote_ingress_rejects_bad_secret_before_reading_body():
    middleware_class = _remote_ingress_class()
    calls = {"app": 0, "receive": 0}
    sent: list[dict] = []

    async def app(scope, receive, send):
        calls["app"] += 1

    async def receive():
        calls["receive"] += 1
        return {"type": "http.request", "body": b"attacker body", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/edit",
        "headers": [(b"x-gen-secret", b"wrong"), (b"content-length", b"999999999")],
    }
    asyncio.run(middleware_class(app, secret="right", max_body_bytes=8)(scope, receive, send))
    assert sent[0]["status"] == 401
    assert calls == {"app": 0, "receive": 0}


def test_remote_ingress_rejects_oversized_authenticated_body_before_parsing():
    middleware_class = _remote_ingress_class()
    called = False
    sent: list[dict] = []

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        raise AssertionError("body must not be consumed")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/edit",
        "headers": [(b"x-gen-secret", b"right"), (b"content-length", b"9")],
    }
    asyncio.run(middleware_class(app, secret="right", max_body_bytes=8)(scope, receive, send))
    assert sent[0]["status"] == 413
    assert called is False


def test_remote_request_models_have_field_and_collection_bounds():
    source = API_SRC
    assert "Prompt = Annotated[str, Field(max_length=2000)]" in source
    assert "images_b64: list[EncodedImage] = Field(min_length=1, max_length=3)" in source
    assert "mask_b64: EncodedImage" in source
    assert 'client_job_id: str = Field(default="", max_length=128)' in source
    assert '@app.post("/inpaint")' in source
    # The pipeline classes live with the loaders, not with the routes.
    assert "QwenImageEditInpaintPipeline" in PIPELINES_SRC
    assert "edit_inpaint" in source


def test_remote_dependencies_are_complete_hash_locked_without_cuda_shadow_packages():
    lock = (REPO_ROOT / "scripts" / "remote-gpu-requirements.lock").read_text()
    inputs = (REPO_ROOT / "scripts" / "remote-gpu-requirements.in").read_text()
    logical = [line for line in lock.splitlines() if line and not line.startswith(("#", " "))]
    requested = [line for line in inputs.splitlines() if line and not line.startswith("#")]
    assert logical
    assert {line.removesuffix(" \\") for line in logical} == set(requested)
    assert all("==" in line and line.endswith(" \\") for line in logical)
    assert lock.count("--hash=sha256:") >= len(logical)
    for forbidden in ("torch==", "numpy==", "nvidia-", "triton=="):
        assert forbidden not in lock
    # The hashed install is a build step now, not something the worker does to
    # itself on a metered GPU.
    assert "--require-hashes" in DOCKERFILE_SRC
    assert "--only-binary=:all:" in DOCKERFILE_SRC
    assert "--no-deps" in DOCKERFILE_SRC
    # torch and numpy come from the CUDA base image, never from the lock, so the
    # lock can never quietly replace a working CUDA build.
    assert "diffusers==0.36.0" not in DOCKERFILE_SRC




def test_the_app_and_the_worker_fingerprint_the_same_thing(tmp_path):
    """/health reports `worker.build_id()`; the app compares it with
    `local_build_id()`. If the two ever disagree the app calls *every* worker
    stale, which is worse than not checking - the warning stops meaning
    anything. They must hash the same files, in the same order, byte for byte.
    """
    from backend.app.remote_gpu_client import local_build_id

    assert build_id() == local_build_id()
    assert build_id() == build_id()          # stable across calls

    # And it must actually move when the source does.
    import hashlib

    def fingerprint(files):
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()[:12]

    real = sorted(WORKER.glob("*.py"))
    assert fingerprint(real) == build_id()
    edited = tmp_path / "api.py"
    edited.write_text((WORKER / "api.py").read_text() + "\n# edit\n")
    assert fingerprint([p for p in real if p.name != "api.py"] + [edited]) != build_id()


def test_config_payload_covers_every_knob_remote_gpu_reads():
    """Any cfg(...) key the worker reads must be shipped by the config generator, or the two
    sides fall back to different defaults. That is how first-block cache ended up
    permanently on remotely (local default 0.0, Remote GPU default 0.05)."""
    source = API_SRC
    read_keys = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_cfg" and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    # Against the real payload, not a hand-copied list — copying it is exactly
    # what went stale when the LTX engine was added.
    shipped = set(gen.remote_gpu_config())
    assert read_keys <= shipped, (
        f"the worker reads keys the config generator never ships: {read_keys - shipped}"
    )


# --- worker pure helpers -------------------------------------------------
# worker.pipelines cannot be imported: it imports torch at module
# scope. Pulling a single function out of its source keeps the pure logic
# testable without any of that, and without a second copy to drift.
def _extract(func_name: str, ns: dict | None = None):
    import ast

    tree = ast.parse(PIPELINES_SRC)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == func_name)
    scope: dict = ns or {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<remote_gpu>", "exec"), scope)
    return scope[func_name]


_LIGHTNING_FILES = [
    "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors",
    "qwen_image_edit_2511_fp8_e4m3fn_scaled.safetensors",
    "Qwen-Image-2512-Lightning-4steps-V1.0-bf16.safetensors",
    "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors",
    "Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors",
    "qwen_image_2512_fp8_e4m3fn_scaled.safetensors",
    "README.md",
]


def _picker(monkeypatch, files=None):
    """_pick_lightning_weight with the Hub stubbed out.

    Patched on the real module, not swapped in sys.modules: the function does
    `from huggingface_hub import HfApi` at CALL time, so a temporary module
    swap is already undone by then.
    """
    import huggingface_hub

    class _Api:
        def list_repo_files(self, repo, **_kwargs):
            return _LIGHTNING_FILES if files is None else files

    monkeypatch.setattr(huggingface_hub, "HfApi", _Api)
    return _extract("_pick_lightning_weight",
                    {"config": type("C", (), {"model_revision": staticmethod(lambda _r: None)})})


def test_base_pipeline_does_not_get_the_edit_adapter(monkeypatch):
    """The bug this exists for: load_lora_weights() with no weight_name let
    diffusers pick, and it picked the Edit adapter for the base image pipeline.
    It only surfaced as a warning line in the notebook log."""
    pick = _picker(monkeypatch)
    chosen = pick("lightx2v/Qwen-Image-2512-Lightning", "image")
    assert "edit" not in chosen.lower()


def test_edit_pipeline_gets_the_edit_adapter(monkeypatch):
    pick = _picker(monkeypatch)
    assert "edit" in pick("lightx2v/Qwen-Image-Edit-2511-Lightning", "edit").lower()


def test_full_checkpoints_are_never_selected(monkeypatch):
    """Two ~20 GB fp8 full checkpoints live in that repo. They are not LoRAs,
    and diffusers was free to choose one."""
    pick = _picker(monkeypatch)
    for kind in ("image", "edit"):
        repo = ("lightx2v/Qwen-Image-Edit-2511-Lightning" if kind == "edit"
                else "lightx2v/Qwen-Image-2512-Lightning")
        assert "fp8_e4m3fn_scaled" not in pick(repo, kind)


def test_prefers_four_steps_bf16_and_the_newest_version(monkeypatch):
    """Speed requests are clamped to 4 steps by the backend, bf16 is half the
    download for the same result here, and the newest available version wins."""
    chosen = _picker(monkeypatch)("lightx2v/Qwen-Image-2512-Lightning", "image")
    assert chosen == "Qwen-Image-2512-Lightning-4steps-V1.0-bf16.safetensors"


def test_returns_none_when_nothing_matches_so_the_caller_can_warn(monkeypatch):
    """A repo with no recognisable Lightning weight must not silently load
    something arbitrary — the caller falls back and says so."""
    pick = _picker(monkeypatch, files=["model.safetensors", "README.md"])
    assert pick("some/repo", "image") is None


# ---- container deployment contract -----------------------------------------
# The worker was written for a notebook: install at startup, tunnel out, keep
# state beside the script. A rented GPU inverts all three. These pin the parts
# that let one file serve both without the container path regressing silently.

DOCKERFILE = REPO_ROOT / "docker" / "remote-gpu" / "Dockerfile"


def _lift(source: str, *names: str) -> dict:
    """Execute named top-level functions out of a worker module's source.

    `worker.api`, `worker.pipelines` and `worker.config` cannot be imported
    here - the first two pull torch at module scope, which conftest's _HEAVY
    guard forbids, and config raises at import without a real secret. Lifting
    the definitions keeps the assertion pointed at shipped code rather than a
    copy that drifts. `worker.runtime` needs none of this and is imported.
    """
    wanted = [n for n in ast.parse(source).body
              if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in wanted} == set(names), "a lifted function is gone"
    ns: dict = {"os": os, "re": re, "Path": pathlib.Path}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<worker>", "exec"), ns)
    return ns


@pytest.mark.parametrize("env,expected", [
    ({}, False),                                # a plain VM publishes nothing itself
    ({"RUNPOD_POD_ID": "abc123"}, True),        # pod proxy terminates TLS in front
    ({"RUNPOD_ENDPOINT_ID": "e1"}, True),       # serverless endpoint likewise
])
def test_managed_host_detection(monkeypatch, tmp_path, env, expected):
    for key in ("RUNPOD_POD_ID", "RUNPOD_ENDPOINT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runtime, "_RUNPOD_ENV_FILE", tmp_path / "absent")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert runtime.is_managed_host() is expected


def test_runpod_is_detected_without_the_env_var(monkeypatch, tmp_path):
    """Measured on a live pod 2026-09-20: Runpod exports RUNPOD_POD_ID into
    /etc/rp_environment and sources it from the interactive shell profile, so a
    worker started by ssh/nohup/systemd - exactly what docs/runpod.md tells an
    operator to do - sees no RUNPOD_POD_ID in its environment at all.

    Reading the environment alone got this wrong for every realistic launch, and
    the startup banner then printed no address to connect to.
    """
    for key in ("RUNPOD_POD_ID", "RUNPOD_ENDPOINT_ID"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "rp_environment"
    env_file.write_text(
        'export RUNPOD_DC_ID="EU-SE-1"\n'
        "export RUNPOD_POD_ID='k4w9biw2diwp1u'\n"
        'export RUNPOD_GPU_COUNT="1"\n'
    )
    monkeypatch.setattr(runtime, "_RUNPOD_ENV_FILE", env_file)
    assert runtime.runpod_pod_id() == "k4w9biw2diwp1u"
    assert runtime.is_managed_host() is True
    # Verified against the live pod: this is the address it actually served on.
    assert runtime.public_url(8000) == "https://k4w9biw2diwp1u-8000.proxy.runpod.net"

    monkeypatch.setenv("RUNPOD_POD_ID", "from-env")
    assert runtime.runpod_pod_id() == "from-env"


def test_a_plain_host_advertises_no_address(monkeypatch, tmp_path):
    """Empty, not a guess: on a VM the operator supplies the route themselves."""
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)
    monkeypatch.setattr(runtime, "_RUNPOD_ENV_FILE", tmp_path / "absent")
    assert runtime.public_url(8000) == ""


def test_config_only_mode_ships_no_secrets(tmp_path, monkeypatch):
    """An image has to be publishable. The baked config carries the reproducible
    half — model slots and reviewed revision pins — and nothing that authenticates."""
    monkeypatch.setattr(gen.settings, "hf_token", "hf_SECRETVALUE", raising=False)
    monkeypatch.setattr(gen.settings, "remote_gpu_shared_secret",
                        "s3cr3tvalue", raising=False)
    out = tmp_path / "remote-gpu-config.json"
    gen.write_config(out)

    import json

    payload = json.loads(out.read_text())
    assert payload["config"]["model_revisions"], "revision pins are what make the image reproducible"
    # No fingerprint here: /health reports the package's own, so a config file
    # cannot claim a build the code is not running.
    assert "build" not in payload
    raw = out.read_text()
    assert "hf_SECRETVALUE" not in raw and "s3cr3tvalue" not in raw
    # Not 0600: an image layer read by another uid must still be able to open it.
    assert stat.S_IMODE(out.stat().st_mode) & stat.S_IRGRP


def test_image_never_bakes_the_secret_bearing_worker():
    """Secrets must never enter an image layer, where a push publishes them.
    Copying that into an image would put them in a layer, where a later push
    publishes them. The image takes the tracked template plus runtime env."""
    body = DOCKERFILE.read_text()
    # Only the directives that actually put bytes in a layer. Matching the whole
    # file would flag the comment explaining why these are excluded — which is
    # exactly what the first version of this test did.
    sources = [part
               for line in body.splitlines()
               if line.strip().split(" ")[0] in {"COPY", "ADD"}
               for part in line.split()[1:-1]
               if not part.startswith("--")]
    assert "worker" in sources
    for forbidden in ("remote_gpu_filled.py", ".env", "output/runtime_settings.json"):
        assert not any(forbidden in src for src in sources), \
            f"the image must not carry {forbidden}"
    assert 'CMD ["python", "-m", "worker"]' in body, "the image must run the package"


def test_image_carries_torchs_non_obvious_system_dependency():
    """torch needs libgomp at *import*, and a slim base does not ship it.

    The image dropped the nvidia/cuda base because torch's cu128 wheels already
    bundle CUDA - measured at ~4.4 GB of duplication - but that base also
    happened to provide OpenMP. Removing this line fails nothing at build time
    and then breaks every worker on its first import.
    """
    body = DOCKERFILE_SRC
    assert "libgomp1" in body
    # git is equally load-bearing: _prepare_hidream pins the runner by commit.
    assert "git" in body


def test_baked_image_config_is_the_weakest_source(monkeypatch):
    """Precedence must be: environment > baked image config > default.

    A build-time default has to stay overridable at deploy time, or the same
    image could not be pointed at a different model without rebuilding it —
    which is most of the reason to build one.
    """
    ns = _lift(CONFIG_SRC, "cfg")
    ns["_FILE_CONFIG"] = {"video_model": "baked/Wan"}
    cfg = ns["cfg"]

    monkeypatch.delenv("VIDEO_MODEL", raising=False)
    assert cfg("video_model", "fallback") == "baked/Wan"

    monkeypatch.setenv("VIDEO_MODEL", "deployed/Wan")
    assert cfg("video_model", "fallback") == "deployed/Wan", "env must beat the image"

    assert cfg("unset_key", "fallback") == "fallback"


def test_legacy_config_keys_still_resolve(monkeypatch):
    """The app's generated config still emits the `a100_*` names, and a worker
    that stopped reading them would silently fall back to its own defaults."""
    ns = _lift(CONFIG_SRC, "cfg")
    ns["_FILE_CONFIG"] = {"a100_image_model": "legacy/Qwen"}
    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    monkeypatch.delenv("A100_IMAGE_MODEL", raising=False)
    assert ns["cfg"]("image_model", "fallback", legacy="a100_image_model") == "legacy/Qwen"


def test_falsy_config_values_still_win_over_the_default():
    """`or` chaining here would resurrect a bug the comment in _cfg names:
    PREVIEW_EVERY=0 must be able to turn previews off."""
    ns = _lift(CONFIG_SRC, "cfg")
    ns["_FILE_CONFIG"] = {"preview_every": 0, "fbcache_threshold": 0.0}
    assert ns["cfg"]("preview_every", "2") == "0"
    assert float(ns["cfg"]("fbcache_threshold", "0.05")) == 0.0


def test_hidream_is_advertised_only_where_it_can_run():
    """The published image ships torch 2.8; HiDream-O1 needs 2.10. Advertising
    the slot on the strength of config alone offered a model whose every job
    failed in the first second (measured on an A100, 2026-09-26)."""
    ns = _lift(PIPELINES_SRC, "torch_at_least")
    at_least = ns["torch_at_least"]
    assert not at_least("2.8.0+cu128", (2, 10))
    assert at_least("2.10.0+cu130", (2, 10))
    assert at_least("2.11.1", (2, 10))
    assert not at_least("garbage", (2, 10))
    assert "HIDREAM_MIN_TORCH = (2, 10)" in PIPELINES_SRC
    assert "if pipelines.hidream_supported():" in API_SRC
    assert 'features.append("image_hidream")' in API_SRC
    assert "if config.IMAGE_MODEL_HIDREAM:\n        features.append" not in API_SRC
    # Refuse before cloning third-party code that could never run here.
    loader = PIPELINES_SRC[PIPELINES_SRC.index("def _load_hidream"):]
    assert loader.index("torch_at_least(") < loader.index("_prepare_hidream()")
