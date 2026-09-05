"""Runtime settings: read/update the Remote GPU URL and other UI-tweakable values.

These layer on top of `.env` and persist to `output/runtime_settings.json`.
Secrets (HF token) are never returned in full — only a masked hint.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import ROOT, settings
from ..remote_gpu_client import remote_gpu_health

router = APIRouter(tags=["settings"])


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 12 else "set"


def _secret_is_set(secret: str) -> bool:
    return secret.strip().lower() not in {"", "change-me-to-anything", "changeme", "secret", "test"}


class SettingsUpdate(BaseModel):
    remote_gpu_base_url: str | None = None
    remote_gpu_shared_secret: str | None = None
    embed_metadata: bool | None = None
    embed_provenance: bool | None = None


def _validated_remote_gpu_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Remote GPU URL must be a public https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(
            status_code=400, detail="Remote GPU URL cannot contain credentials, query, or fragment")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise HTTPException(status_code=400, detail="Remote GPU URL must not point at this machine or LAN")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass  # a public hostname; request-time networking still resolves it
    else:
        if not addr.is_global:
            raise HTTPException(status_code=400, detail="Remote GPU URL must use a public address")
    return value


def _authority(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.netloc.rstrip(".").lower()


def _settings_payload() -> dict:
    ov = settings.load_overrides()
    remote_secret = (
        ov.get("remote_gpu_shared_secret")
        or ov.get("colab_shared_secret")
        or settings.remote_gpu_shared_secret
    )
    from ..generators import variants

    models = [
        {
            "label": f"{'This machine' if variant.lane == 'local' else 'A100 image'} · "
                     f"{variant.name.title()}",
            "id": variants.repo_of(variant),
            # `variants` retains "colab" as a private compatibility key; do
            # not let that historical implementation detail leak into the API.
            "lane": "local" if variant.lane == "local" else "remote_gpu",
            "kind": "image",
        }
        for lane in variants.LANES
        for variant in variants.available(lane)
    ]
    extras = (
        ("A100 image editing", settings.qwen_edit_model, "remote_gpu", "image-edit"),
        ("A100 video · Wan", settings.video_model, "remote_gpu", "video"),
        ("A100 video · Hunyuan", settings.hunyuan_video_model, "remote_gpu", "video"),
        ("A100 video · LTX", settings.ltx_video_model, "remote_gpu", "video"),
    )
    models.extend(
        {"label": label, "id": model, "lane": lane, "kind": kind}
        for label, model, lane, kind in extras if model
    )
    return {
        "remote_gpu_base_url": settings.effective_remote_gpu_url,
        "remote_gpu_shared_secret_set": _secret_is_set(str(remote_secret)),
        "hf_token_hint": _mask(settings.hf_token),
        "embed_metadata": settings.effective_bool("embed_metadata"),
        "embed_provenance": settings.effective_bool("embed_provenance"),
        "configuration_warning": settings.override_warning,
        "models": models,
        "local": {"quant": settings.local_quant, "offload": settings.local_offload},
        "storage": {
            "output": str(settings.output_path.relative_to(ROOT)),
            "huggingface": str(settings.hf_home_path.relative_to(ROOT)),
            "weights": str(settings.weights_path.relative_to(ROOT)),
            "loras": str(settings.loras_dir.relative_to(ROOT)),
            "runtime": str(settings.runtime_path.relative_to(ROOT)),
        },
    }


@router.get("/settings")
async def read_settings() -> dict:
    return _settings_payload()


@router.post("/settings")
async def update_settings(payload: SettingsUpdate) -> dict:
    ov = settings.load_overrides()
    previous_url = settings.effective_remote_gpu_url
    connection_changed = payload.remote_gpu_base_url is not None or bool(payload.remote_gpu_shared_secret)
    if payload.remote_gpu_base_url is not None:
        new_url = _validated_remote_gpu_url(payload.remote_gpu_base_url)
        if (new_url and _authority(new_url) != _authority(previous_url)
                and not _secret_is_set(payload.remote_gpu_shared_secret or "")):
            raise HTTPException(
                status_code=400,
                detail="Enter the Remote GPU shared secret again when changing its host",
            )
        ov["remote_gpu_base_url"] = new_url
        ov.pop("colab_base_url", None)
    if payload.remote_gpu_shared_secret:
        ov["remote_gpu_shared_secret"] = payload.remote_gpu_shared_secret.strip()
        ov.pop("colab_shared_secret", None)
    for name in ("embed_metadata", "embed_provenance"):
        if (value := getattr(payload, name)) is not None:
            ov[name] = value
    settings.save_overrides(ov)
    result = {"ok": True, **_settings_payload()}
    if connection_changed:
        result["remote_gpu"] = await remote_gpu_health()
    return result
