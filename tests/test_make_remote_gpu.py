"""make_remote_gpu injection contract.

remote_gpu.py is the one file with no runtime coverage, and `make remote_gpu` is how it
reaches the A100. These tests pin the two things that actually broke: injection
anchored on exact source text, and config keys silently missing from the payload.
"""
from __future__ import annotations

import ast
import asyncio
import os
import stat

import pytest

from scripts import make_remote_gpu


def _src(hf='HF_TOKEN = ""', secret='REMOTE_GPU_SHARED_SECRET = ""',
         cfg="REMOTE_GPU_CONFIG: dict = {}") -> str:
    return (
        f"# header\n{hf}\n{secret}\n{cfg}\n"
        'REMOTE_GPU_REQUIREMENTS_LOCK = ""\n\nimport os\nprint(os)\n'
    )


def test_locates_every_declared_placeholder():
    src = _src() + '\nREMOTE_GPU_BUILD = ""\n'
    assert set(make_remote_gpu._assignment_lines(src)) == set(make_remote_gpu._TARGETS)


@pytest.mark.parametrize("variant", [
    "HF_TOKEN = ''",                 # single quotes
    'HF_TOKEN   =   ""',             # extra spacing
    'HF_TOKEN: str = ""',            # annotated
])
def test_injection_survives_reformatting(variant):
    """The old regex anchored on `^HF_TOKEN = ""$`, so any of these broke
    `make remote_gpu` with 'could not find the placeholder'."""
    spans = make_remote_gpu._assignment_lines(_src(hf=variant))
    assert "HF_TOKEN" in spans


def test_missing_placeholder_is_the_only_hard_failure():
    src = _src().replace('HF_TOKEN = ""\n', "")
    assert "HF_TOKEN" not in make_remote_gpu._assignment_lines(src)


def test_booleans_render_as_python_not_json():
    """The bug this exists to prevent: json.dumps emits lowercase `false`, which
    is a valid Python IDENTIFIER. The file parses cleanly, uploads fine, and then
    dies with NameError at import on the A100 — after the runtime spins up and
    the models start downloading. Nothing in the config was boolean until
    enable_sage_attention, which is how it slipped through."""
    line = make_remote_gpu._render("REMOTE_GPU_CONFIG", {"enable_sage_attention": False, "preview_every": 0})
    assert "false" not in line and "False" in line
    ns: dict = {}
    exec(line, ns)
    assert ns["REMOTE_GPU_CONFIG"]["enable_sage_attention"] is False
    assert ns["REMOTE_GPU_CONFIG"]["preview_every"] == 0


def test_every_injected_value_is_a_pure_literal():
    """Guards the whole payload, not just booleans: anything that is not a
    literal would resolve as a name at import time."""
    import ast

    from backend.app.config import settings

    for name, value in (("HF_TOKEN", settings.hf_token),
                        ("REMOTE_GPU_SHARED_SECRET", "abc123"),
                        ("REMOTE_GPU_CONFIG", {"a": True, "b": None, "c": 1.5, "d": "x"}),
                        ("REMOTE_GPU_REQUIREMENTS_LOCK",
                         "demo==1 \\" + "\n  --hash=sha256:" + "a" * 64)):
        node = ast.parse(make_remote_gpu._render(name, value)).body[0]
        ast.literal_eval(node.value)  # raises if it is not a literal


def test_render_produces_parseable_assignments():
    for name, value in [("HF_TOKEN", "hf_abc"),
                        ("REMOTE_GPU_SHARED_SECRET", "s3cr3t"),
                        ("REMOTE_GPU_CONFIG", {"a": 1, "b": "x"})]:
        line = make_remote_gpu._render(name, value)
        ast.parse(line)  # must be valid Python on its own
    # the annotation is preserved so the injected file still type-checks
    assert make_remote_gpu._render("REMOTE_GPU_CONFIG", {}).startswith("REMOTE_GPU_CONFIG: dict = ")


def test_render_escapes_values_that_would_break_the_source():
    nasty = 'a"b\\c\nd'
    line = make_remote_gpu._render("HF_TOKEN", nasty)
    ns: dict = {}
    exec(line, ns)
    assert ns["HF_TOKEN"] == nasty


def test_generated_remote_worker_is_atomic_and_private(tmp_path):
    path = tmp_path / "remote_gpu_filled.py"
    make_remote_gpu._write_private(path, "SECRET = 'first'\n")
    make_remote_gpu._write_private(path, "SECRET = 'second'\n")
    assert path.read_text() == "SECRET = 'second'\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _remote_ingress_class():
    tree = ast.parse(make_remote_gpu.SRC.read_text())
    wanted = {"_RemoteBodyTooLarge", "_RemoteIngressMiddleware"}
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "MAX_REMOTE_BODY_BYTES"
                    for target in node.targets))
        or (isinstance(node, ast.ClassDef) and node.name in wanted)
    ]
    scope: dict = {}
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
    source = make_remote_gpu.SRC.read_text()
    assert "Prompt = Annotated[str, Field(max_length=2000)]" in source
    assert "images_b64: list[EncodedImage] = Field(min_length=1, max_length=3)" in source
    assert 'client_job_id: str = Field(default="", max_length=128)' in source


def test_remote_dependencies_are_complete_hash_locked_without_cuda_shadow_packages():
    source = make_remote_gpu.SRC.read_text()
    lock = (make_remote_gpu.ROOT / "scripts" / "remote-gpu-requirements.lock").read_text()
    inputs = (make_remote_gpu.ROOT / "scripts" / "remote-gpu-requirements.in").read_text()
    logical = [line for line in lock.splitlines() if line and not line.startswith(("#", " "))]
    requested = [line for line in inputs.splitlines() if line and not line.startswith("#")]
    assert logical
    assert {line.removesuffix(" \\") for line in logical} == set(requested)
    assert all("==" in line and line.endswith(" \\") for line in logical)
    assert lock.count("--hash=sha256:") >= len(logical)
    for forbidden in ("torch==", "numpy==", "nvidia-", "triton=="):
        assert forbidden not in lock
    assert '"--require-hashes"' in source
    assert '"--only-binary=:all:"' in source
    assert '"--no-deps"' in source
    assert '"diffusers==0.36.0"' not in source


def test_real_remote_gpu_py_still_carries_every_placeholder():
    """Guards the actual file: if someone renames or removes one of these,
    `make remote_gpu` breaks, and this says so at test time instead. Compared against
    _TARGETS rather than a hardcoded set so adding a placeholder cannot leave
    this test asserting yesterday's list."""
    spans = make_remote_gpu._assignment_lines(make_remote_gpu.SRC.read_text())
    assert set(spans) == set(make_remote_gpu._TARGETS)


def test_build_id_is_stable_and_ignores_secrets():
    """The backend recomputes this from its own remote_gpu.py to detect a stale
    notebook, so it must depend only on the source, never on injected values."""
    src = make_remote_gpu.SRC.read_text()
    assert make_remote_gpu.build_id(src) == make_remote_gpu.build_id(src)
    assert make_remote_gpu.build_id(src) != make_remote_gpu.build_id(src + "\n# edit\n")

    from backend.app.remote_gpu_client import local_build_id

    assert make_remote_gpu.build_id(src) == local_build_id()


def test_config_payload_covers_every_knob_remote_gpu_reads():
    """Any _cfg(...) key remote_gpu.py reads must be shipped by make_remote_gpu, or the two
    sides fall back to different defaults. That is how first-block cache ended up
    permanently on remotely (local default 0.0, Remote GPU default 0.05)."""
    source = make_remote_gpu.SRC.read_text()
    read_keys = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_cfg" and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    # Against the real payload, not a hand-copied list — copying it is exactly
    # what went stale when the LTX engine was added.
    shipped = set(make_remote_gpu.remote_gpu_config())
    assert read_keys <= shipped, (
        f"remote_gpu.py reads keys make_remote_gpu never ships: {read_keys - shipped}"
    )


# --- remote_gpu.py pure helpers -------------------------------------------------
# remote_gpu.py cannot be imported: it pip-installs and imports torch at module
# scope. Pulling a single function out of its source keeps the pure logic
# testable without any of that, and without a second copy to drift.
def _extract(func_name: str, ns: dict | None = None):
    import ast

    src = make_remote_gpu.SRC.read_text()
    tree = ast.parse(src)
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
    return _extract("_pick_lightning_weight", {"_model_revision": lambda _repo: None})


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
