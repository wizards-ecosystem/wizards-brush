"""Generate remote_gpu_filled.py: remote_gpu.py with HF_TOKEN / REMOTE_GPU_SHARED_SECRET and the
local model config injected, so real keys never live in git.

Run via `make remote-gpu`, then copy the generated remote_gpu_filled.py to the
Remote GPU you operate.

Injection is AST-located, not regex-matched. The previous version anchored on the
exact source text `^NAME = ""$`, so reformatting three lines of remote_gpu.py — adding
a type annotation, changing quote style, running a formatter — broke `make remote-gpu`
with a confusing "could not find the placeholder" error. Locating the assignment
in the parse tree and rewriting its line survives all of that; it only fails if
the assignment genuinely stops existing, which is the case worth failing on.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.model_sources import MODEL_REVISIONS

SRC = ROOT / "remote_gpu.py"
DST = ROOT / "remote_gpu_filled.py"

# Placeholder assignments to overwrite, in remote_gpu.py's CELL 1 header.
_TARGETS = (
    "HF_TOKEN",
    "REMOTE_GPU_SHARED_SECRET",
    "REMOTE_GPU_CONFIG",
    "REMOTE_GPU_BUILD",
    "REMOTE_GPU_REQUIREMENTS_LOCK",
)

_WEAK_SECRETS = {"", "change-me-to-anything", "changeme", "secret", "test"}


def _assignment_lines(source: str) -> dict[str, tuple[int, int]]:
    """name -> (start_line, end_line), 1-indexed inclusive, for each target
    module-level assignment. Handles both `X = ...` and `X: dict = ...`."""
    tree = ast.parse(source)
    found: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        name = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name in _TARGETS and name not in found:
            found[name] = (node.lineno, node.end_lineno or node.lineno)
    return found


def _render(name: str, value: object) -> str:
    """The replacement source line. REMOTE_GPU_CONFIG keeps its `: dict` annotation so
    the file still type-checks after injection.

    repr(), not json.dumps(): they agree on strings and numbers but not on
    booleans, and JSON's lowercase `false` is a *valid Python identifier*. It
    parses cleanly and then dies with NameError at import time on the A100 —
    after the upload, after the runtime spins up. Nothing in the config was
    boolean until enable_sage_attention, which is exactly how this slipped in.
    """
    if name == "REMOTE_GPU_CONFIG":
        return f"REMOTE_GPU_CONFIG: dict = {value!r}"
    return f"{name} = {value!r}"


def build_id(source: str) -> str:
    """Short fingerprint of remote_gpu.py, so /health can report which build is live.

    Hashes the source as it exists in the repo, BEFORE injection, so the backend
    can compute the same value from its own copy. Secrets never enter it."""
    import hashlib

    return hashlib.sha256(source.encode()).hexdigest()[:12]


def remote_gpu_config() -> dict:
    """Everything the Remote GPU side reads via _cfg(), in one place.

    Exposed rather than inlined into main() so the test that checks remote_gpu.py
    reads no key this omits can compare against the real payload instead of a
    hand-copied list — which is precisely what went stale when LTX was added.
    """
    return {
            "a100_image_model": settings.a100_image_model,
            "a100_image_model_hidream": settings.a100_image_model_hidream,
            "a100_image_model_alt": settings.a100_image_model_alt,
            "qwen_edit_model": settings.qwen_edit_model,
            "video_model": settings.video_model,
            "hunyuan_video_model": settings.hunyuan_video_model,
            "ltx_video_model": settings.ltx_video_model,
            "image_lightning_lora": settings.image_lightning_lora,
            "edit_lightning_lora": settings.edit_lightning_lora,
            "video_lightning_lora": settings.video_lightning_lora,
            "preview_every": settings.preview_every,
            "fbcache_threshold": settings.fbcache_threshold,
            "enable_sage_attention": settings.enable_sage_attention,
            "model_revisions": MODEL_REVISIONS,
        }


def _write_private(path: pathlib.Path, content: str) -> None:
    """Atomically replace a generated secret-bearing file with mode 0600."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False,
    ) as pending_file:
        if os.name == "posix":
            os.fchmod(pending_file.fileno(), 0o600)
        pending_file.write(content)
        pending_file.flush()
        os.fsync(pending_file.fileno())
        pending = pathlib.Path(pending_file.name)
    try:
        os.replace(pending, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    finally:
        pending.unlink(missing_ok=True)


def main() -> None:
    source = SRC.read_text()
    spans = _assignment_lines(source)
    missing = [t for t in _TARGETS if t not in spans]
    if missing:
        sys.exit(f"!! remote_gpu.py has no module-level assignment for: {', '.join(missing)}")

    # Everything the Remote GPU side needs to mirror the local .env. Keys omitted here
    # fall through to remote_gpu.py's own defaults, which is how FBCACHE_THRESHOLD and
    # ENABLE_SAGE_ATTENTION used to diverge: local default 0.0 (off) vs Remote GPU
    # default 0.05 (on), with no way to turn it off from `make remote-gpu`.
    values: dict[str, object] = {
        "HF_TOKEN": settings.hf_token,
        "REMOTE_GPU_SHARED_SECRET": settings.remote_gpu_shared_secret,
        "REMOTE_GPU_BUILD": build_id(source),
        "REMOTE_GPU_CONFIG": remote_gpu_config(),
        "REMOTE_GPU_REQUIREMENTS_LOCK": (
            ROOT / "scripts" / "remote-gpu-requirements.lock"
        ).read_text(encoding="utf-8"),
    }

    lines = source.split("\n")
    # Rewrite bottom-up so earlier spans keep their line numbers.
    for name in sorted(_TARGETS, key=lambda n: spans[n][0], reverse=True):
        start, end = spans[name]
        lines[start - 1:end] = [_render(name, values[name])]
    text = "\n".join(lines)

    # The generated file must still be valid Python — catch a bad injection here,
    # not after a 20-minute model download on the A100.
    try:
        ast.parse(text)
    except SyntaxError as e:
        sys.exit(f"!! injection produced invalid Python: {e}")

    # Parsing is not enough. JSON's `false`/`true`/`null` are valid Python
    # identifiers, so a JSON-encoded value parses fine and then raises NameError
    # at import. Check that what we injected is a pure literal.
    #
    # By line number, not by name: remote_gpu.py legitimately reassigns these later
    # (`HF_TOKEN = HF_TOKEN or os.environ.get(...)`), and those are expressions
    # by design. Only the placeholder lines we rewrote are ours to vouch for.
    injected_lines = {spans[name][0] for name in _TARGETS}
    for node in ast.parse(text).body:
        if node.lineno not in injected_lines or not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        if node.value is None:
            continue
        try:
            ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            sys.exit(f"!! line {node.lineno} was not injected as a Python literal — "
                     "a JSON encoding would NameError on the A100.")

    if (settings.remote_gpu_shared_secret or "").strip().lower() in _WEAK_SECRETS:
        sys.exit("!! REMOTE_GPU_SHARED_SECRET in .env is unset or a default placeholder.\n"
                 "   remote_gpu.py refuses to start with one, because the tunnel URL is\n"
                 "   public. Set a real value first: openssl rand -hex 16")

    _write_private(DST, text)
    print(f"Wrote {DST.name} (gitignored). Copy it to your Remote GPU and run: python {DST.name}")


if __name__ == "__main__":
    main()
