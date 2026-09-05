"""Prompt + seed helpers shared by image/video generators.

Pure functions, no heavy imports — safe to use in any handler.
  • inline variants: "a {red|blue|green} car" sweeps combinations across a batch
  • file wildcards: "__animal__" substitutes a seed-keyed line from wildcards/animal.txt
  • negative-prompt defaults: fill a sensible model-appropriate negative when blank
  • seed variation: increment / fixed / random across a batch
"""
from __future__ import annotations

import random
import re
import secrets
from collections.abc import Iterable
from functools import reduce
from pathlib import Path

from .config import ROOT
from .presets import FACE_NEGATIVE, IMAGE_NEGATIVE, WAN_NEGATIVE_ZH

_SEED_MAX = 2**32 - 1
# Only treat braces containing a pipe as a wildcard group; leave plain "{x}" alone.
_WILDCARD = re.compile(r"\{([^{}]*\|[^{}]*)\}")
_FILE_WC = re.compile(r"__([a-zA-Z0-9_\-]+)__")
_WEIGHT_SUFFIX = re.compile(r"^(.*):[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*$", re.DOTALL)

MAX_PROMPT_CHARS = 2000
MAX_WILDCARD_FILE_BYTES = 1024 * 1024
MAX_WILDCARD_ENTRIES = 10_000
MAX_WILDCARD_LINE_CHARS = 1000
MAX_WILDCARD_DEPTH = 4

WILDCARDS_DIR = ROOT / "wildcards"

# name -> (mtime_ns, size, lines). Reloaded when the file changes.
_WC_CACHE: dict[str, tuple[int, int, list[str]]] = {}


def clamp_seed(seed: int) -> int:
    return max(0, min(int(seed), _SEED_MAX))


def load_wildcard(name: str) -> list[str]:
    """Non-empty stripped lines of wildcards/<name>.txt (mtime-cached). [] if missing."""
    path = WILDCARDS_DIR / f"{name}.txt"
    try:
        stat = path.stat()
    except OSError:
        _WC_CACHE.pop(name, None)
        return []
    cached = _WC_CACHE.get(name)
    if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_WILDCARD_FILE_BYTES + 1)
    except OSError:
        return []
    if len(raw) > MAX_WILDCARD_FILE_BYTES:
        raw = raw[:MAX_WILDCARD_FILE_BYTES]
    lines = [
        line.strip()[:MAX_WILDCARD_LINE_CHARS]
        for line in raw.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ][:MAX_WILDCARD_ENTRIES]
    _WC_CACHE[name] = (stat.st_mtime_ns, stat.st_size, lines)
    return lines


def _sub_file_wildcards(prompt: str, seed: int | None, index: int = 0) -> str:
    """Replace each __name__ with a seed-keyed pick from its file. Deterministic per
    (seed, name, occurrence) so rerun-with-same-seed reproduces the same prompt.
    Unknown names are left as literal text — a typo must never fail a job."""
    if "__" not in (prompt or ""):
        return prompt or ""
    occurrence: dict[str, int] = {}

    def _pick(m: re.Match) -> str:
        name = m.group(1)
        lines = load_wildcard(name)
        if not lines:
            return m.group(0)
        idx = occurrence.get(name, 0)
        occurrence[name] = idx + 1
        if seed is None:
            return random.choice(lines)
        # Include the batch index separately from the image seed. In fixed-seed
        # mode the pixels intentionally share a seed, but `{a|b}` still varies
        # per item; file wildcards should follow the same prompt semantics.
        rng = random.Random(f"{seed}:{index}:{name}:{idx}")
        return rng.choice(lines)

    out = prompt
    for _depth in range(MAX_WILDCARD_DEPTH):
        expanded = _FILE_WC.sub(_pick, out)
        if expanded == out:
            break
        out = expanded[:MAX_PROMPT_CHARS]
        if "__" not in out:
            break
    return out[:MAX_PROMPT_CHARS]


def strip_a1111_emphasis(prompt: str) -> str:
    """Turn A1111 emphasis into plain text for encoders without that syntax.

    This deliberately does not *implement* weighting. It prevents punctuation
    and numeric weights from becoming literal T5/Qwen tokens while preserving
    escaped brackets and unmatched punctuation as ordinary text.
    """
    out: list[str] = []
    stack: list[tuple[str, int]] = []
    i = 0
    while i < len(prompt):
        char = prompt[i]
        if char == "\\" and i + 1 < len(prompt):
            out.append(prompt[i + 1])
            i += 2
            continue
        if char in "([":
            stack.append((char, len(out)))
            out.append(char)
        elif char in ")]" and stack and ((stack[-1][0], char) in {("(", ")"), ("[", "]")}):
            opener, start = stack.pop()
            content = "".join(out[start + 1:])
            if opener == "(" and (weighted := _WEIGHT_SUFFIX.match(content)):
                content = weighted.group(1)
            del out[start:]
            out.extend(content)
        else:
            out.append(char)
        i += 1
    return "".join(out)


def prepare_prompt(
    prompt: str, index: int = 0, seed: int | None = None, *, syntax: str = "literal",
) -> str:
    """Expand project syntax, optionally normalizing imported A1111 emphasis."""
    expanded = expand_prompt(prompt, index, seed)
    if syntax == "a1111":
        expanded = strip_a1111_emphasis(expanded)
    return expanded[:MAX_PROMPT_CHARS]


def _variant_picks(groups: list[str], index: int) -> list[str]:
    """Mixed-radix walk of the cartesian product: one option pick per `{a|b|c}`
    group for variant `index`, so the first N indexes yield distinct combinations."""
    picks: list[str] = []
    n = max(0, int(index))
    for opts in (g.split("|") for g in groups):
        picks.append(opts[n % len(opts)].strip())
        n //= len(opts)
    return picks


def _sub_picks(prompt: str, picks: list[str]) -> str:
    """Replace each `{a|b|c}` group in order with its pre-computed pick."""
    it = iter(picks)
    return _WILDCARD.sub(lambda _m: next(it), prompt)


def expand_prompt(prompt: str, index: int = 0, seed: int | None = None) -> str:
    """Expand `{a|b|c}` groups for batch variant `index`, then substitute
    `__file__` wildcards keyed by `seed`. With no wildcards the prompt is
    returned unchanged."""
    groups = _WILDCARD.findall(prompt or "")
    out = prompt or ""
    if groups:
        out = _sub_picks(out, _variant_picks(groups, index))
    return _sub_file_wildcards(out, seed, index)[:MAX_PROMPT_CHARS]


def has_wildcards(prompt: str) -> bool:
    return bool(_WILDCARD.search(prompt or ""))


def count_variants(prompt: str) -> int:
    """Number of distinct {a|b|c} combinations (file wildcards don't multiply)."""
    groups = _WILDCARD.findall(prompt or "")
    if not groups:
        return 1
    return reduce(lambda acc, g: acc * len(g.split("|")), groups, 1)


def expand_all(prompt: str, cap: int = 32) -> list[str]:
    """Every {a|b|c} combination (capped), with file wildcards left in place —
    they get seed-keyed per job at generation time."""
    groups = _WILDCARD.findall(prompt or "")
    if not groups:
        return [prompt or ""]
    total = count_variants(prompt)
    n = min(total, max(1, cap))
    indexes: Iterable[int]
    if n == total:
        indexes = range(total)
    elif n == 1:
        indexes = (0,)
    else:
        # Evenly sample the whole mixed-radix walk. Taking range(cap) pins the
        # slowest-changing trailing groups to their first option, so a "capped"
        # study never studies them at all.
        indexes = tuple(round(i * (total - 1) / (n - 1)) for i in range(n))
    return [_sub_picks(prompt, _variant_picks(groups, i))[:MAX_PROMPT_CHARS]
            for i in indexes]


def list_wildcards() -> list[dict]:
    """Available wildcard files with entry counts (for the UI)."""
    if not WILDCARDS_DIR.exists():
        return []
    out = []
    for p in sorted(WILDCARDS_DIR.glob("*.txt")):
        out.append({"name": p.stem, "count": len(load_wildcard(p.stem))})
    return out


def safe_wildcard_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", (name or "").strip())[:64]
    if not clean:
        raise ValueError("wildcard name required")
    return clean


def save_wildcard(name: str, items: list[str]) -> Path:
    WILDCARDS_DIR.mkdir(parents=True, exist_ok=True)
    path = WILDCARDS_DIR / f"{safe_wildcard_name(name)}.txt"
    clean = [s.strip()[:MAX_WILDCARD_LINE_CHARS] for s in items if s.strip()]
    if len(clean) > MAX_WILDCARD_ENTRIES:
        raise ValueError(f"wildcards are limited to {MAX_WILDCARD_ENTRIES} entries")
    payload = "\n".join(clean) + "\n"
    if len(payload.encode("utf-8")) > MAX_WILDCARD_FILE_BYTES:
        raise ValueError("wildcard file is larger than 1 MB")
    path.write_text(payload, encoding="utf-8")
    _WC_CACHE.pop(path.stem, None)
    return path


def delete_wildcard(name: str) -> bool:
    path = WILDCARDS_DIR / f"{safe_wildcard_name(name)}.txt"
    _WC_CACHE.pop(path.stem, None)
    if path.exists():
        path.unlink()
        return True
    return False


def resolve_negative(kind: str, negative: str, auto: bool = True) -> str:
    """Fill a model-appropriate default negative when the field is blank and auto is on."""
    if (negative or "").strip():
        return negative
    if not auto:
        return ""
    if kind in ("t2v", "i2v", "long_video"):
        return WAN_NEGATIVE_ZH
    if kind in ("img2img", "inpaint"):
        return FACE_NEGATIVE
    return IMAGE_NEGATIVE


def seed_for(base_seed: int, index: int, mode: str = "increment") -> int:
    """Per-batch-item seed. increment (default) = base+i; fixed = base; random = fresh."""
    if mode == "fixed":
        return clamp_seed(base_seed)
    if mode == "random":
        return secrets.randbelow(_SEED_MAX)
    return clamp_seed(base_seed + index)
