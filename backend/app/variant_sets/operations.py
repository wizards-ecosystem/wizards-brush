"""Which existing operations a Variant Set may drive, and how.

A set never has an image-generation path of its own. Each operation here is an
existing job kind with its existing registered handler, and a child's params
are built by the same function the generate route uses (``images.build_params``)
— so a child is indistinguishable from a job someone queued by hand, and lanes,
model affinity, rerun, restart and asset replay treat it exactly the same.

What this table adds is only what a set needs to know that the registry does
not say: how many source images the operation consumes, whether it needs a
mask, and so whether a later stage can feed it the previous stage's output.
Controls, their options and their defaults still come from the registry.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..models import JobKind

# Controls a set supplies itself, so a recipe may not set them as plain params:
# the prompt and negative come from templates, one combination is one image,
# and seeds follow the set's seed policy.
SET_CONTROLLED = frozenset({
    "prompt", "negative_prompt", "batch", "combinatorial", "seed", "seed_mode",
})


@dataclass(frozen=True)
class Operation:
    kind: str                           # persisted JobKind value
    min_sources: int
    max_sources: int
    mask: Literal["none", "required"]
    note: str

    @property
    def takes_source(self) -> bool:
        """Whether a later stage can hand this operation the previous output."""
        return self.max_sources > 0

    def build(self, raw: dict[str, Any], source_paths: Sequence[str],
              mask_path: str | None) -> dict[str, Any]:
        from ..routers.images import build_params

        return build_params(self.kind, raw, source_paths, mask_path)

    def handler(self) -> Callable:
        from ..routers.common import get_handler

        handler = get_handler(self.kind)
        if handler is None:  # images.py registers every kind here at import
            raise RuntimeError(f"no handler is registered for {self.kind}")
        return handler


OPERATIONS: dict[str, Operation] = {op.kind: op for op in (
    Operation(JobKind.image_edit.value, 1, 3, "none",
              "Edits by instruction; every variant shares the same 1-3 reference images."),
    Operation(JobKind.img2img.value, 1, 1, "none",
              "Transforms the source; strength sets how far each variant departs."),
    Operation(JobKind.inpaint.value, 1, 1, "required",
              "Regenerates only a masked region; everything outside it is kept."),
    Operation(JobKind.outpaint.value, 1, 1, "none",
              "Extends the canvas around the source."),
    Operation(JobKind.control_local.value, 1, 1, "none",
              "Keeps the source's structure (edges, depth or pose)."),
    Operation(JobKind.image_local.value, 0, 0, "none",
              "Generates from the prompt alone on the local GPU."),
    Operation(JobKind.image_colab.value, 0, 0, "none",
              "Generates from the prompt alone on the Remote GPU."),
)}


def registry_specs() -> dict[str, dict[str, Any]]:
    """Registry entries by kind, as the frontend sees them right now."""
    from ..generators.registry import registry

    return {str(spec["kind"]): spec for spec in registry()}


def available(specs: dict[str, dict[str, Any]] | None = None) -> list[Operation]:
    """Supported operations the live registry currently offers.

    ControlNet only appears when ENABLE_CONTROLNET is on, and the same gate
    applies here: an operation the registry does not list cannot be chosen.
    """
    live = registry_specs() if specs is None else specs
    return [op for kind, op in OPERATIONS.items() if kind in live]


def controls_for(spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The registry controls a recipe may set for this operation, by name."""
    return {str(c["name"]): c for c in spec.get("controls", [])
            if c.get("name") not in SET_CONTROLLED}
