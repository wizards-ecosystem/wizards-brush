"""Safe template substitution for Variant Sets.

Templates turn one combination of axis values into text: the effective prompt,
the negative prompt, an output name. The whole language is one construct:

    {{axis}}        the axis's content for this value when the recipe maps it,
                    otherwise the value itself
    {{axis.value}}  always the value itself, even when content is mapped

plus, in naming templates only, the built-ins ``{{_index}}``, ``{{_key}}`` and
``{{_stage}}``. There are no expressions, filters, conditionals, attribute
access or function calls, so nothing a recipe contains can execute.

Every case that could be ambiguous is decided up front and rejected with a
message rather than resolved quietly:

* **Unknown placeholder** — a name that is not an axis at this point in the
  recipe is an error, not left literal and not replaced with nothing. A typo
  in an instruction should never reach a paid generation.
* **Missing axis** — a placeholder naming an axis that a *later* stage defines
  is an error that says so; that value does not exist yet.
* **Nested/recursive expansion** — substitution is a single pass over the
  template. Replacement text is never re-scanned, and values and content that
  contain ``{{`` or ``}}`` are refused at validation, so there is nothing that
  could look like a placeholder after substitution either.
* **Malformed braces** — a ``{{`` or ``}}`` that is not part of a well-formed
  placeholder is an error. There is no escape syntax; a literal double brace
  has no use in a prompt.

Single braces are untouched, so the prompt engine's own ``{a|b}`` choices and
``__wildcard__`` files keep working inside a template exactly as elsewhere.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

PLACEHOLDER = re.compile(r"\{\{\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\.(?P<attr>value))?\s*\}\}")
BUILTINS = frozenset({"_index", "_key", "_stage"})


class TemplateError(ValueError):
    """A template that cannot be rendered as written. The message is user-facing."""


@dataclass(frozen=True)
class Slot:
    """What one placeholder can resolve to."""

    value: str
    content: str | None = None   # the recipe's mapped text for this value, if any

    @property
    def text(self) -> str:
        return self.content if self.content is not None else self.value


def placeholders(template: str) -> list[str]:
    """Names referenced by `template`, in order of first appearance."""
    seen: list[str] = []
    for match in PLACEHOLDER.finditer(template or ""):
        if match.group("name") not in seen:
            seen.append(match.group("name"))
    return seen


def check(
    template: str, *, field: str, available: Collection[str],
    later: Collection[str] = (), builtins: bool = False,
) -> list[str]:
    """Validate `template` and return the names it references.

    `available` are the axes known at this point; `later` are axes a later
    stage defines, which get their own message because the fix is different.
    """
    text = template or ""
    residue = PLACEHOLDER.sub("", text)
    if "{{" in residue or "}}" in residue:
        at = residue.find("{{") if "{{" in residue else residue.find("}}")
        raise TemplateError(
            f"{field}: malformed placeholder near {residue[max(0, at - 12):at + 16]!r}; "
            "write {{axis}} or {{axis.value}} using an axis name"
        )
    names = placeholders(text)
    for name in names:
        if name in BUILTINS:
            if not builtins:
                raise TemplateError(
                    f"{field}: {{{{{name}}}}} is only available in naming templates"
                )
            continue
        if name in available:
            continue
        if name in later:
            raise TemplateError(
                f"{field}: {{{{{name}}}}} is defined by a later stage, so it has no value here"
            )
        raise TemplateError(f"{field}: unknown placeholder {{{{{name}}}}}")
    return names


def render(template: str, slots: Mapping[str, Slot], builtins: Mapping[str, str] | None = None) -> str:
    """Substitute every placeholder in one pass. Assumes `check` has passed.

    `re.sub` never rescans its own output, which is the whole guarantee against
    recursive expansion: content containing "{{x}}" (already refused at
    validation) could not be expanded here even if it slipped through.
    """
    extra = builtins or {}

    def substitute(match: re.Match[str]) -> str:
        name = match.group("name")
        if name in extra:
            return extra[name]
        slot = slots.get(name)
        if slot is None:
            raise TemplateError(f"unknown placeholder {{{{{name}}}}}")
        return slot.value if match.group("attr") == "value" else slot.text

    return PLACEHOLDER.sub(substitute, template or "")
