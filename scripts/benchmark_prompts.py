"""Shared, validated prompt-suite input for local and A100 model audits."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BenchmarkPrompt:
    id: str
    category: str
    prompt: str
    width: int
    height: int
    seed: int
    smoke: bool = False

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


def load_suite(path: Path, *, smoke_only: bool = False,
               selected: set[str] | None = None,
               seed_override: int | None = None) -> tuple[str, list[BenchmarkPrompt]]:
    """Read a prompt suite and reject ambiguous or unsafe benchmark inputs."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("prompts"), list):
        raise ValueError("prompt suite must be an object containing a prompts list")
    name = str(raw.get("name") or path.stem).strip()
    prompts: list[BenchmarkPrompt] = []
    seen: set[str] = set()
    for item in raw["prompts"]:
        if not isinstance(item, dict):
            raise ValueError("every prompt-suite row must be an object")
        prompt_id = str(item.get("id") or "").strip()
        text = str(item.get("prompt") or "").strip()
        if not prompt_id or prompt_id in seen:
            raise ValueError(f"prompt ids must be non-empty and unique: {prompt_id!r}")
        seen.add(prompt_id)
        if not text:
            raise ValueError(f"prompt {prompt_id!r} is empty")
        width, height = int(item.get("width") or 0), int(item.get("height") or 0)
        if not 256 <= width <= 2048 or not 256 <= height <= 2048:
            raise ValueError(f"prompt {prompt_id!r} dimensions must be 256..2048")
        if width % 16 or height % 16:
            raise ValueError(f"prompt {prompt_id!r} dimensions must be divisible by 16")
        if smoke_only and not bool(item.get("smoke")):
            continue
        if selected and prompt_id not in selected:
            continue
        prompts.append(BenchmarkPrompt(
            id=prompt_id,
            category=str(item.get("category") or "general"),
            prompt=text,
            width=width,
            height=height,
            seed=int(seed_override if seed_override is not None else item.get("seed", 12345)),
            smoke=bool(item.get("smoke")),
        ))
    unknown = (selected or set()) - seen
    if unknown:
        raise ValueError(f"unknown prompt id(s): {', '.join(sorted(unknown))}")
    if not prompts:
        raise ValueError("prompt selection is empty")
    return name, prompts


def load_single(path: Path, *, seed: int, width: int = 768,
                height: int = 1024) -> tuple[str, list[BenchmarkPrompt]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{path} is empty")
    return path.stem, [BenchmarkPrompt(
        id=path.stem,
        category="custom",
        prompt=text,
        width=width,
        height=height,
        seed=seed,
        smoke=True,
    )]
