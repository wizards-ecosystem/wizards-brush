"""Axis expansion: named axes in, deterministic combinations out.

An axis is a name and an ordered list of values. That is all this module knows.
Four properties are load-bearing, and each has a test:

1. **Stable order.** Combinations come out in declaration order with the last
   axis varying fastest — exactly nested loops, exactly ``itertools.product``.
   Adding a value to the last axis never renumbers anything before it.
2. **Stable identity.** A combination's canonical key is built from axis names
   and value *identifiers* (see :func:`slug`), never from its position, so a key
   survives a reordered or extended recipe and can be persisted and compared.
3. **No ambiguity.** Two values of one axis that reduce to the same identifier
   would produce two combinations that cannot be told apart by key or filename,
   so they are rejected rather than resolved by some tie-break the user never
   sees. Axis names are compared case-insensitively for the same reason.
4. **No silent truncation.** The count is computed arithmetically before a
   single combination is built, and a count over the cap is an error that says
   so. Nothing is ever quietly dropped from the end.

The cap is deliberately far above the X/Y grid's 36 cells and the batch cap of
8: a production set of sizes x locales x states is routinely hundreds.
"""
from __future__ import annotations

import itertools
import math
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

# An axis name is used as a template placeholder, so it must be an identifier.
# A leading underscore is reserved for built-ins ({{_index}}, {{_key}}), which
# is why the first character must be a letter.
AXIS_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

MAX_STAGES = 4
MAX_AXES_PER_STAGE = 12
MAX_VALUES_PER_AXIS = 256
MAX_VALUE_CHARS = 120
MAX_SLUG_CHARS = 64

# The configured cap is clamped to this. It bounds a single request's database
# writes and queued jobs; anything larger is a batch-processing job, not a set.
HARD_MAX_COMBINATIONS = 10_000
DEFAULT_MAX_COMBINATIONS = 1_000

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Everything that is not a letter or digit becomes one hyphen, underscores
# included — so a slug never contains "_", and joining slugs with "_" (the
# default naming scheme) can never make two different combinations collide.
_NON_IDENT = re.compile(r"(?:[^\w]|_)+")


class ExpansionError(ValueError):
    """A definition that cannot be expanded as written. The message is shown to
    the user as-is, so it names the axis and value involved."""


@dataclass(frozen=True)
class Axis:
    name: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class Combination:
    """One point in the product of a stage's axes."""

    index: int                          # position in the deterministic order, 0-based
    pairs: tuple[tuple[str, str], ...]  # ((axis, value), ...) in axis order

    def as_dict(self) -> dict[str, str]:
        return dict(self.pairs)

    @property
    def key(self) -> str:
        return canonical_key(self.pairs)


def slug(value: str) -> str:
    """The identifier form of a value: what keys and default names are built from.

    NFKC-normalised and case-folded, so values that differ only by width or case
    — which a case-insensitive filesystem would merge anyway — share one
    identifier and are caught as duplicates. Letters and digits of any script
    survive; everything else collapses to a single hyphen.
    """
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return _NON_IDENT.sub("-", text).strip("-")[:MAX_SLUG_CHARS].strip("-")


def canonical_key(pairs: Iterable[tuple[str, str]]) -> str:
    """``axis=identifier`` pairs joined by commas, in axis order.

    Unambiguous without escaping: identifiers contain only letters, digits and
    single hyphens, and axis names only letters, digits and underscores.
    """
    return ",".join(f"{name}={slug(value)}" for name, value in pairs)


def clean_value(raw: Any, axis: str) -> str:
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        raise ExpansionError(f"axis '{axis}': values must be text, got {raw!r}")
    value = str(raw).strip()
    if not value:
        raise ExpansionError(f"axis '{axis}' has an empty value")
    if len(value) > MAX_VALUE_CHARS:
        raise ExpansionError(
            f"axis '{axis}': value {value[:24]!r}… is longer than {MAX_VALUE_CHARS} characters; "
            "keep values short and put longer text in the axis's content map"
        )
    if _CONTROL.search(value):
        raise ExpansionError(f"axis '{axis}': value {value!r} contains a control character")
    if "{{" in value or "}}" in value:
        raise ExpansionError(
            f"axis '{axis}': value {value!r} contains '{{{{' or '}}}}'; placeholders are "
            "never expanded inside a value"
        )
    if not slug(value):
        raise ExpansionError(
            f"axis '{axis}': value {value!r} has no letters or digits to identify it by"
        )
    return value


def make_axis(name: Any, values: Sequence[Any]) -> Axis:
    """One validated axis. Raises ExpansionError naming the problem."""
    label = str(name if name is not None else "").strip()
    if not AXIS_NAME.fullmatch(label):
        raise ExpansionError(
            f"axis name {label!r} must start with a letter and contain only letters, "
            "digits and underscores (at most 32 characters)"
        )
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ExpansionError(f"axis '{label}': values must be a list")
    if not values:
        raise ExpansionError(f"axis '{label}' needs at least one value")
    if len(values) > MAX_VALUES_PER_AXIS:
        raise ExpansionError(
            f"axis '{label}' has {len(values)} values; the limit is {MAX_VALUES_PER_AXIS}"
        )
    cleaned: list[str] = []
    seen: dict[str, str] = {}
    for raw in values:
        value = clean_value(raw, label)
        ident = slug(value)
        if ident in seen:
            first = seen[ident]
            raise ExpansionError(
                f"axis '{label}': value {value!r} appears twice" if first == value else
                f"axis '{label}': values {first!r} and {value!r} both reduce to the "
                f"identifier {ident!r}, so their variants could not be told apart"
            )
        seen[ident] = value
        cleaned.append(value)
    return Axis(label, tuple(cleaned))


def check_unique_names(axes: Iterable[Axis]) -> None:
    """Reject two axes whose names differ only by case, anywhere in a recipe.

    Checked across every stage, not per stage: a staged variant's key
    accumulates every stage's selections, and two axes named `color` would make
    that key — and every template placeholder — ambiguous.
    """
    seen: dict[str, str] = {}
    for axis in axes:
        folded = axis.name.casefold()
        if folded in seen:
            raise ExpansionError(
                f"axis name '{axis.name}' is used twice"
                + (f" (as '{seen[folded]}')" if seen[folded] != axis.name else "")
            )
        seen[folded] = axis.name


def count(axes: Sequence[Axis]) -> int:
    """How many combinations these axes produce. No axes is one combination: the
    stage runs once (per parent, for a derived stage)."""
    return math.prod(len(axis.values) for axis in axes)


def stage_totals(per_stage_counts: Sequence[int]) -> list[int]:
    """Items per stage when each stage derives from every item of the previous one.

    Stage k has the product of stages 0..k combinations: every stage-k
    combination is applied to every item the previous stage produced.
    """
    totals: list[int] = []
    running = 1
    for n in per_stage_counts:
        running *= n
        totals.append(running)
    return totals


def configured_cap() -> int:
    """The per-set cap from VARIANT_MAX_COMBINATIONS, clamped to a sane range."""
    from ..config import settings

    try:
        value = int(settings.variant_max_combinations)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_COMBINATIONS
    return max(1, min(value, HARD_MAX_COMBINATIONS))


def check_cap(per_stage_counts: Sequence[int], cap: int) -> int:
    """Total items across every stage, or ExpansionError if that exceeds `cap`.

    Pure arithmetic on the counts, so a request for ten billion combinations is
    refused without allocating anything.
    """
    totals = stage_totals(per_stage_counts)
    total = sum(totals)
    if total > cap:
        detail = " + ".join(str(t) for t in totals) if len(totals) > 1 else str(total)
        raise ExpansionError(
            f"this recipe expands to {detail} = {total} generations; the limit is {cap} "
            "(VARIANT_MAX_COMBINATIONS). Remove values or split it into several sets."
            if len(totals) > 1 else
            f"this recipe expands to {total} combinations; the limit is {cap} "
            "(VARIANT_MAX_COMBINATIONS). Remove values or split it into several sets."
        )
    return total


def combinations(axes: Sequence[Axis]) -> Iterator[Combination]:
    """Every combination, in declaration order with the last axis fastest."""
    names = [axis.name for axis in axes]
    for index, values in enumerate(itertools.product(*(axis.values for axis in axes))):
        yield Combination(index, tuple(zip(names, values, strict=True)))
