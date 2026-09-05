"""Tracking which parameters a handler actually read.

Two problems, one mechanism.

**Saved metadata that lies.** A generation records its parameters into the
asset's `meta_json`. If a parameter was sent but never read — because the handler
does not implement it, or a branch skipped it — recording it anyway means the
metadata claims a setting took effect when it did not. Re-running from that
metadata then produces something different, and nothing explains why.

**Controls that silently do nothing.** `generators/registry.py` declares controls;
handlers read them with `params.get(...)`. Nothing connects the two. A control can
be added, rendered, changed by the user, and never read, with no error anywhere.
That is a real bug class and it is invisible.

`TrackedParams` is a dict that records every key read. At metadata time, keys that
were never read are moved out of the recorded parameters and listed separately.
So the metadata describes what actually happened, and an ignored control becomes
a reported fact.

Adapted from SwarmUI's `ParamsQueried` (MIT).
"""
from __future__ import annotations

from typing import Any

# Keys that are legitimately never read by a handler. Routing and bookkeeping
# rather than generation inputs — reporting them would be noise that trains
# people to ignore the report.
ALWAYS_UNUSED: frozenset[str] = frozenset({
    "grid", "grid_id", "group_id",
    "image_path", "mask_path",      # consumed by the router before the handler
    "combinatorial",                # expanded into sibling jobs at submit time
    "model_variant",                # resolved into model_key at submit time
    "seed_mode",                    # applied by item_seed_prompt per batch item
    "auto_negative",                # applied by resolve_negative
})


class TrackedParams(dict):
    """A params dict that remembers which keys were read.

    Subclasses dict rather than wrapping it so every existing handler works
    unchanged — `params.get(...)`, `params["x"]`, `in`, iteration and `**params`
    all behave exactly as before.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Not a dict key, so it never leaks into iteration or serialization.
        object.__setattr__(self, "_read", set())

    @property
    def read_keys(self) -> set[str]:
        return set(self._read)  # type: ignore[attr-defined]

    def _mark(self, key: Any) -> None:
        if isinstance(key, str):
            self._read.add(key)  # type: ignore[attr-defined]

    def __getitem__(self, key: Any) -> Any:
        self._mark(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._mark(key)
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        # A membership test is a read: `if "mask_path" in params` is exactly how
        # an optional input gets consumed.
        self._mark(key)
        return super().__contains__(key)

    def pop(self, key: Any, *args: Any) -> Any:
        self._mark(key)
        return super().pop(key, *args)

    def unused(self) -> list[str]:
        """Keys never read, excluding the ones that never are. Sorted."""
        return sorted(k for k in self
                      if k not in self._read  # type: ignore[attr-defined]
                      and k not in ALWAYS_UNUSED
                      and not k.startswith("_"))


def split_metadata(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(parameters that took effect, names of ones that did not).

    A plain dict has no read history, so everything is treated as used. That
    keeps this safe to call from any handler, tracked or not.
    """
    if not isinstance(params, TrackedParams):
        return dict(params), []
    unused = params.unused()
    if not unused:
        return dict(params), []
    ignored = set(unused)
    return {k: v for k, v in params.items() if k not in ignored}, unused
