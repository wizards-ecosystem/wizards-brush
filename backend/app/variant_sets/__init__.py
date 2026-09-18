"""Variant Sets: reference-driven, multidimensional generation as a durable object.

A *recipe* is a reusable definition: named axes, the existing operation to run,
templates that turn one combination of axis values into effective parameters,
and optional finishing, validation and naming. A *set* is one execution of a
recipe: an immutable snapshot of it plus one item per combination, each with its
own state machine and, once runnable, an ordinary child job.

Nothing in this package knows what an axis represents. An axis is a name and an
ordered list of values; hairstyles, materials, locales and layouts are all the
same thing here. Keep it that way — domain vocabulary belongs in recipes, which
are user data, never in code.

Modules, in dependency order:

* ``expansion`` — axes, canonical keys, deterministic Cartesian order, caps. Pure.
* ``templates`` — safe ``{{axis}}`` substitution. Pure.
* ``naming``    — deterministic output names and collision detection. Pure.
* ``recipe``    — the recipe schema, normalization and validation.
* ``operations``— which existing job kinds a set may drive, and how.
* ``store``     — persistence of sets and items.
* ``service``   — orchestration: create, submit, complete, retry, cancel, restart.

"Variant" here is unrelated to ``model_variant``, the model-picker control; the
code always says *variant set*, *variant recipe* or *variant item* to keep the
two apart.
"""
