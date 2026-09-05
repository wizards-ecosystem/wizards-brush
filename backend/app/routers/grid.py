"""X/Y grid generation: fan a parameter sweep out into ordinary sibling jobs
grouped by group_id, and serve the assembled grid for the viewer.

Each cell is a normal job (cancelable / rerunnable / reorderable); the grid is
just a query over the group.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..models import AssetRead, JobKind
from ..utils.seeds import resolve_seed
from .common import get_handler, submit_fanout
from .images import _common_params

router = APIRouter(tags=["grid"])

# txt2img kinds only in v1 — img2img/inpaint cells would need a shared upload.
_GRID_KINDS = {JobKind.image_local.value, JobKind.image_colab.value}
MAX_CELLS = 36
_NUMERIC_AXES = frozenset({"seed", "steps", "guidance", "strength"})
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_STEP_RANGE = re.compile(
    rf"^\s*(?P<start>{_NUMBER})\s*-\s*(?P<end>{_NUMBER})\s*"
    rf"\(\s*\+\s*(?P<step>{_NUMBER})\s*\)\s*$")
_COUNT_RANGE = re.compile(
    rf"^\s*(?P<start>{_NUMBER})\s*-\s*(?P<end>{_NUMBER})\s*"
    r"\[\s*(?P<count>\d+)\s*\]\s*$")

class Axis(BaseModel):
    param: str
    values: list[Any]
    sr_search: str | None = None  # for param == "prompt_sr"


class GridReq(BaseModel):
    kind: str
    payload: dict[str, Any]
    x: Axis
    y: Axis | None = None


def _plain_number(value: Decimal, *, integer: bool) -> int | float:
    if integer:
        if value != value.to_integral_value():
            raise HTTPException(status_code=400, detail="seed/steps ranges need whole numbers")
        return int(value)
    return float(value)


def _expand_numeric_values(axis: Axis) -> list[Any]:
    """Expand compact study ranges, bounded before allocating a large list."""
    integer = axis.param in {"seed", "steps"}
    expanded: list[Any] = []
    for raw in axis.values:
        text = str(raw).strip()
        step_match = _STEP_RANGE.fullmatch(text)
        count_match = _COUNT_RANGE.fullmatch(text)
        try:
            if step_match:
                start = Decimal(step_match.group("start"))
                end = Decimal(step_match.group("end"))
                step = Decimal(step_match.group("step"))
                if step <= 0 or end < start:
                    raise HTTPException(
                        status_code=400,
                        detail=f"axis '{axis.param}' range needs an ascending end and positive step",
                    )
                value = start
                while value <= end:
                    expanded.append(_plain_number(value, integer=integer))
                    if len(expanded) > MAX_CELLS:
                        raise HTTPException(
                            status_code=400,
                            detail=f"axis '{axis.param}' range expands beyond the {MAX_CELLS}-cell cap",
                        )
                    value += step
                continue
            if count_match:
                start = Decimal(count_match.group("start"))
                end = Decimal(count_match.group("end"))
                count = int(count_match.group("count"))
                if count < 2 or count > MAX_CELLS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"axis '{axis.param}' range count must be 2..{MAX_CELLS}",
                    )
                if end < start:
                    raise HTTPException(status_code=400, detail=f"axis '{axis.param}' range must ascend")
                interval = (end - start) / Decimal(count - 1)
                expanded.extend(
                    _plain_number(start + interval * i, integer=integer)
                    for i in range(count)
                )
                continue
        except InvalidOperation:
            raise HTTPException(
                status_code=400, detail=f"axis '{axis.param}' has an invalid numeric range {text!r}"
            ) from None
        expanded.append(raw)
    return expanded


def _apply_axis(payload: dict[str, Any], axis: Axis, value: Any) -> None:
    """Apply one axis value to the RAW payload (before _common_params derives
    steps/dims/etc. from it) — sweeping e.g. `quality` or `aspect` on the derived
    params would be a silent no-op because handlers never re-derive them."""
    if axis.param == "prompt_sr":
        search = (axis.sr_search or "").strip()
        if not search:
            raise HTTPException(status_code=400, detail="prompt_sr axis needs sr_search")
        payload["prompt"] = str(payload.get("prompt", "")).replace(search, str(value))
    elif axis.param == "seed":
        payload["seed"] = resolve_seed(value)
    elif axis.param == "steps":
        payload["quality"] = "Custom"
        payload["steps"] = int(_num(axis.param, value))
    elif axis.param in ("guidance", "strength"):
        payload[axis.param] = _num(axis.param, value)
    elif axis.param == "speed_mode":
        payload["speed_mode"] = str(value).lower() in ("1", "true", "yes", "on")
    else:
        from ..generators.registry import GRID_SWEEPABLE
        if axis.param not in GRID_SWEEPABLE:
            raise HTTPException(status_code=400, detail=f"cannot sweep '{axis.param}'")
        payload[axis.param] = value


def _materialize_axis(axis: Axis, payload: dict[str, Any], *, lane: str) -> Axis:
    """Resolve labels once and reject a grid whose columns would lie."""
    from ..generators import variants
    from ..generators.registry import GRID_SWEEPABLE

    if axis.param != "prompt_sr" and axis.param not in GRID_SWEEPABLE:
        raise HTTPException(status_code=400, detail=f"cannot sweep '{axis.param}'")
    raw_values = _expand_numeric_values(axis) if axis.param in _NUMERIC_AXES else list(axis.values)
    values = [resolve_seed(v) for v in raw_values] if axis.param == "seed" else raw_values
    labels = [str(v) for v in values]
    if len(set(labels)) != len(labels):
        raise HTTPException(status_code=400, detail=f"axis '{axis.param}' has duplicate values")
    if axis.param == "prompt_sr":
        search = (axis.sr_search or "").strip()
        if not search:
            raise HTTPException(status_code=400, detail="prompt_sr axis needs sr_search")
        if search not in str(payload.get("prompt", "")):
            raise HTTPException(status_code=400, detail=f"prompt does not contain {search!r}")
    if axis.param == "model_variant":
        allowed = set(variants.options(lane))
        bad = [str(v) for v in values if str(v) not in allowed]
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"unknown {lane} model variant(s): {', '.join(bad)}")
    return Axis(param=axis.param, values=values, sr_search=axis.sr_search)


def _num(param: str, value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"axis '{param}' needs numeric values, got {value!r}") from None


class _EnqueueOrder(NamedTuple):
    """How to walk the grid when enqueueing, which is not how it is displayed.

    `outer` and `inner` are value lists; `x_is_outer` says which axis each one
    belongs to. An explicit flag rather than comparing axis objects, because the
    single-axis case has no Y axis to compare against and silently produced the
    wrong assignment when it was inferred.
    """
    outer: list[Any]
    inner: list[Any]
    x_is_outer: bool


def _enqueue_order(x: Axis, y: Axis | None, y_values: list[Any]) -> _EnqueueOrder:
    """Walk order with the more expensive parameter outermost.

    With no Y axis there is nothing to reorder: the pseudo-Y is a single None,
    so X stays the inner loop exactly as before.

    Ties keep X inner, matching the original nesting, so an ordinary grid
    (guidance against steps) enqueues in precisely the order it always did.
    """
    from ..generators.registry import change_weight

    if y is None:
        return _EnqueueOrder([None], list(x.values), x_is_outer=False)
    if change_weight(x.param) > change_weight(y.param):
        return _EnqueueOrder(list(x.values), y_values, x_is_outer=True)
    return _EnqueueOrder(y_values, list(x.values), x_is_outer=False)


@router.post("/generate/grid")
async def generate_grid(req: GridReq) -> dict:
    if req.kind not in _GRID_KINDS:
        raise HTTPException(status_code=400, detail=f"grids support {sorted(_GRID_KINDS)} only")
    if not req.x.values:
        raise HTTPException(status_code=400, detail="x axis needs at least one value")
    if req.y and req.y.param == req.x.param:
        raise HTTPException(status_code=400, detail="x and y axes must use different parameters")
    handler = get_handler(req.kind)
    if handler is None:
        raise HTTPException(status_code=500, detail=f"no handler for {req.kind}")

    lane = "local" if req.kind == JobKind.image_local.value else "colab"
    base_payload = dict(req.payload)
    x_axis = _materialize_axis(req.x, base_payload, lane=lane)
    y_axis = _materialize_axis(req.y, base_payload, lane=lane) if req.y else None
    y_values: list[Any] = y_axis.values if (y_axis and y_axis.values) else [None]
    cells = len(x_axis.values) * len(y_values)
    if cells > MAX_CELLS:
        raise HTTPException(status_code=400, detail=f"{cells} cells exceeds the {MAX_CELLS}-cell cap")

    # Pin the seed on the base payload up front: cells must share it (unless seed
    # IS an axis) or a "random" seed request would differ per cell and confound
    # the comparison the grid exists for.
    swept = {x_axis.param} | ({y_axis.param} if y_axis else set())
    if "seed" not in swept:
        base_payload["seed"] = resolve_seed(base_payload.get("seed"))

    # Enqueue with the EXPENSIVE axis outermost, whatever the user put on X.
    #
    # A grid cell's cost is dominated by whether it forces a model reload. Naive
    # nesting (Y outer, X inner) means putting model_variant on X reloads on
    # every single cell: 16 loads for a 4x4 instead of 4. Ordering by
    # change_weight groups the costly value together and pays for it once per
    # group. Nothing about the grid the user *sees* changes — the cell's
    # coordinates travel in `grid`, and the viewer lays out by those.
    order = _enqueue_order(x_axis, y_axis, y_values)

    variants: list[dict[str, Any]] = []
    for ov in order.outer:
        for iv in order.inner:
            raw = dict(base_payload)
            xv, yv = (ov, iv) if order.x_is_outer else (iv, ov)
            _apply_axis(raw, x_axis, xv)
            if y_axis and yv is not None:
                _apply_axis(raw, y_axis, yv)
            # Derive per cell so quality/aspect/speed_mode axes take real effect.
            p = _common_params(raw, req.kind)
            p["batch"] = 1  # one image per cell
            p["combinatorial"] = False
            p["grid"] = {
                "x": str(xv), "y": "" if yv is None else str(yv),
                "x_param": x_axis.param, "y_param": y_axis.param if y_axis else None,
            }
            variants.append(p)

    # submit_fanout merges variants over base — pass full per-cell params as variants.
    result = await submit_fanout(req.kind, {}, handler, variants)
    return {"group_id": result["group_id"], "job_ids": result["job_ids"]}


@router.get("/grids/{group_id}")
async def get_grid(group_id: str) -> dict:
    jobs = db.list_jobs_by_group(group_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="grid not found")
    x_param = None
    y_param = None
    x_values: list[str] = []
    y_values: list[str] = []
    # Batch-load every cell's asset in one query instead of one SELECT per cell.
    first_ids = [(j.result.get("asset_ids") or [None])[0] for j in jobs]
    assets_by_id = {a.id: a for a in db.get_assets([i for i in first_ids if i])}
    cells = []
    for j, first_id in zip(jobs, first_ids, strict=False):
        g = j.params.get("grid", {})
        x_param = x_param or g.get("x_param")
        y_param = y_param or g.get("y_param")
        x, y = str(g.get("x", "")), str(g.get("y", ""))
        if x not in x_values:
            x_values.append(x)
        if y not in y_values:
            y_values.append(y)
        a = assets_by_id.get(first_id) if first_id else None
        asset = AssetRead.of(a) if a else None
        cells.append({"x": x, "y": y, "job_id": j.id, "status": j.status, "asset": asset})
    return {
        "group_id": group_id, "kind": jobs[0].kind,
        "x_param": x_param, "x_values": x_values,
        "y_param": y_param, "y_values": [v for v in y_values if v != ""] or None,
        "cells": cells,
    }
