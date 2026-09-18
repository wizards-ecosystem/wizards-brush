"""Deterministic output names for Variant Set items.

A name is rendered from a template over the item's axis values, then made safe:

* ``{{axis}}`` is the value's identifier (see ``expansion.slug``): lowercase,
  letters, digits and hyphens. ``{{axis.value}}`` is the value itself with
  filesystem-hostile characters replaced.
* ``/`` in the *template* separates folders inside an export. A value can never
  introduce one — separators inside values are replaced before substitution.
* Every path component is sanitized on its own: control and reserved characters
  become ``_``; leading and trailing dots and spaces are stripped, so ``.`` and
  ``..`` become empty components and are refused rather than resolved; Windows
  device names get a leading underscore. The result can only ever be a relative
  path below the export root.
* The extension comes from the output format, not from the template.

Names are an *output key*, not the file's name on disk: stored files keep the
app's collision-free stamped names, because their URLs are immutable. Nothing
ever parses a name to recover which variant it was — the item row records that.

The default scheme joins every axis identifier with ``_``. Identifiers never
contain ``_``, so the default cannot collide; a custom template can (by leaving
an axis out), which is why collisions are checked before generation and again
before export.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from .expansion import slug
from .templates import Slot, render

MAX_COMPONENT_CHARS = 120
MAX_DEPTH = 4
MAX_TEMPLATE_CHARS = 300
MAX_PREFIX_CHARS = 80

_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"\\|?*]')
_SPACES = re.compile(r"\s+")
_IMAGE_SUFFIX = re.compile(r"\.(?:png|jpe?g|webp)$", re.IGNORECASE)
_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
})


class NamingError(ValueError):
    """A name that cannot be made safe as written. The message is user-facing."""


def default_template(axis_names: Sequence[str]) -> str:
    """Every axis, in declaration order; ``{{_index}}`` when there are none."""
    if not axis_names:
        return "{{_index}}"
    return "_".join(f"{{{{{name}}}}}" for name in axis_names)


def _slots(values: Mapping[str, str]) -> dict[str, Slot]:
    # In a name {{axis}} renders `content` (the identifier) and {{axis.value}}
    # renders `value`. Separators are neutralised here, before substitution, so
    # a value can never add a folder.
    return {
        axis: Slot(value=value.replace("/", "_").replace("\\", "_"), content=slug(value))
        for axis, value in values.items()
    }


def sanitize_component(part: str) -> str:
    text = unicodedata.normalize("NFKC", part)
    text = _UNSAFE.sub("_", text)
    text = _SPACES.sub(" ", text).strip(" .")
    if not text:
        raise NamingError(
            "the naming template produces an empty folder or file name "
            "(for example from '..', a leading '/', or a value that is only punctuation)"
        )
    if len(text) > MAX_COMPONENT_CHARS:
        raise NamingError(
            f"the name component {text[:32]!r}… is longer than {MAX_COMPONENT_CHARS} characters"
        )
    if text.split(".")[0].casefold() in _RESERVED:
        text = "_" + text
    return text


def render_name(
    template: str, values: Mapping[str, str], *, prefix: str = "",
    builtins: Mapping[str, str] | None = None, extension: str = "png",
) -> str:
    """The safe relative export path for one item."""
    text = f"{prefix or ''}{render(template, _slots(values), builtins)}"
    text = _IMAGE_SUFFIX.sub("", text.strip())
    parts = text.split("/")
    if len(parts) > MAX_DEPTH:
        raise NamingError(f"names may be at most {MAX_DEPTH} folders deep")
    clean = [sanitize_component(part) for part in parts]
    clean[-1] = f"{clean[-1]}.{extension}"
    return "/".join(clean)


def find_collisions(names: Mapping[str, str]) -> dict[str, list[str]]:
    """Output names claimed by more than one item: name -> the item keys.

    Compared case-insensitively, because an export unpacked on a case-insensitive
    filesystem would silently merge `Red.png` and `red.png`. A file whose path is
    also used as a folder by another name is reported too.
    """
    by_name: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for key, name in names.items():
        folded = name.casefold()
        by_name.setdefault(folded, []).append(key)
        shown.setdefault(folded, name)
    collisions = {shown[f]: keys for f, keys in by_name.items() if len(keys) > 1}
    folders = {
        "/".join(name.casefold().split("/")[:depth])
        for name in names.values() for depth in range(1, name.count("/") + 1)
    }
    for folded, keys in by_name.items():
        if folded in folders:
            collisions.setdefault(shown[folded], list(keys))
    return collisions
