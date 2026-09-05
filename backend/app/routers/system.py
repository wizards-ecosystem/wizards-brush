"""System status: local GPU, Remote GPU connection health, model registry, presets."""
from __future__ import annotations

import asyncio
import subprocess

from fastapi import APIRouter

from ..config import settings
from ..generators.registry import registry
from ..presets import presets_payload
from ..remote_gpu_client import remote_gpu_health
from ..version import get_version

router = APIRouter(tags=["system"])


def _local_gpu() -> dict:
    """Query the local GPU via nvidia-smi (no torch import needed here)."""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {"available": False}
        rows = []
        for line in out.stdout.splitlines():
            fields = [x.strip() for x in line.split(",")]
            if len(fields) != 5:
                continue
            row_name, row_total, row_used, row_util, row_temp = fields
            rows.append((row_name, int(float(row_total)), int(float(row_used)),
                         int(float(row_util)), int(float(row_temp))))
        if not rows:
            return {"available": False}
        # Generation is single-device today. Report the card the automatic
        # profile should target: the one with the largest memory budget.
        name, total, used, util, temp = max(rows, key=lambda row: row[1])
        return {
            "available": True, "name": name,
            "memory_total_mb": total, "memory_used_mb": used,
            "utilization_pct": util, "temperature_c": temp,
            "device_count": len(rows),
        }
    except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
        return {"available": False}


@router.get("/system")
async def get_system() -> dict:
    # nvidia-smi is a blocking subprocess (up to 5s); keep it off the event loop
    # so a status poll never stalls the jobs WebSocket carrying live progress.
    return {
        # Reported so a bug report can start with a version instead of a guess.
        "version": get_version(),
        "local_gpu": await asyncio.to_thread(_local_gpu),
        "remote_gpu": await remote_gpu_health(),
        "models": {
            "local_image": settings.local_image_model,
            "a100_image": settings.a100_image_model,
            "video": settings.video_model,
        },
    }


@router.get("/backends")
async def get_backends() -> dict:
    """Every backend's health, plus the hardware profile in force.

    One place the UI can ask "where can work run, and what will it do" — for the
    settings page, the startup banner, and the first-run walkthrough.
    """
    from .. import backends, hardware

    # This is the explicit diagnosis refresh, so pay for a live remote health
    # call here. Ordinary registry/page renders keep using the cached snapshot.
    await remote_gpu_health()
    report = backends.health_report()
    return {
        "backends": [
            {
                "id": b.id, "label": b.label, "kind": b.kind.value,
                "health": {
                    "connected": h.connected, "reason": h.reason, "device": h.device,
                    "vram_gb": h.vram_gb, "build": h.build,
                    "build_stale": h.build_stale, "queue_depth": h.queue_depth,
                    "disk_free_gb": h.disk_free_gb,
                    "features": sorted(h.features),
                    "models_loaded": list(h.models_loaded),
                },
                "models": [
                    {"id": m.id, "label": m.label, "kind": m.kind,
                     "ready": m.ready, "status": m.status,
                     "size_gb": m.size_gb, "family": m.family}
                    for m in _safe_catalog(b)
                ],
            }
            for b in backends.all_backends()
            for h in [report[b.id]]
        ],
        "hardware": hardware.describe(),
        "credentials": {"hf_token_set": bool(settings.hf_token)},
    }


def _safe_catalog(backend) -> list:
    """A backend that cannot list its models must not take the page down."""
    try:
        return backend.catalog()
    except Exception:  # noqa: BLE001
        return []


@router.get("/models")
async def get_models() -> list[dict]:
    """Generator registry — drives dynamic control rendering in the UI."""
    return registry()


@router.get("/presets")
async def get_presets() -> dict:
    return presets_payload()
