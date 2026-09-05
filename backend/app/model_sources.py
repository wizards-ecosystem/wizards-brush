"""Repository-reviewed immutable revisions for the project's shipped model IDs.

Custom model slots intentionally remain operator-controlled. A known public
default gets a commit SHA; an unknown repository follows the identifier the
operator explicitly configured.
"""
from __future__ import annotations

MODEL_REVISIONS: dict[str, str] = {
    "Tongyi-MAI/Z-Image-Turbo": "f332072aa78be7aecdf3ee76d5c247082da564a6",
    "Qwen/Qwen-Image-2512": "25468b98e3276ca6700de15c6628e51b7de54a26",
    "HiDream-ai/HiDream-O1-Image": "0b0901d99f200389e138c61946af1185f5f49a13",
    "Qwen/Qwen-Image-Edit-2511": "6f3ccc0b56e431dc6a0c2b2039706d7d26f22cb9",
    "Wan-AI/Wan2.2-TI2V-5B-Diffusers": "b8fff7315c768468a5333511427288870b2e9635",
    "lightx2v/Qwen-Image-2512-Lightning": "a52649c9d0f6e1a248bff13f0df33bb8a2abdb52",
    "lightx2v/Qwen-Image-Edit-2511-Lightning": "d74eba145674fd7e31b949324e148e21e7118abd",
    "florence-community/Florence-2-base-ft": "0b03b6f15a4a211370fb204aee4e7dd48887ea37",
    "openai/clip-vit-base-patch32": "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268",
    "alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union-2.1": (
        "5155fc56d17821007d6f62ac192c09e0f0e72016"
    ),
    "lllyasviel/Annotators": "982e7edaec38759d914a963c48c4726685de7d96",
    "tarn59/pixel_art_style_lora_z_image_turbo": (
        "0a5092d1619664d94a5a36784f92db84b3ae62bd"
    ),
    "renderartist/Classic-Painting-Z-Image-Turbo-LoRA": (
        "e873cc474ce517d49d09ed1f64358ff43660b276"
    ),
    "ostris/z_image_turbo_childrens_drawings": (
        "7fcd66a99149c58741990fca28562a4a581af7a9"
    ),
    "nunchaku-ai/nunchaku-z-image-turbo": "ca6bac69c3b0b2bdd31ca5196bf87c5f2a9eaedf",
}


def revision_for(repo_id: str) -> str | None:
    return MODEL_REVISIONS.get(repo_id)


def hub_revision(repo_id: str) -> dict[str, str]:
    revision = revision_for(repo_id)
    return {"revision": revision} if revision else {}
