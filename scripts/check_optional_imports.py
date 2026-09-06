"""Smoke-test optional processors whose upstream dependency metadata is stale."""

from __future__ import annotations

from importlib.metadata import version


def main() -> None:
    # Imports stay inside the entry point: MediaPipe and controlnet_aux pull in
    # the heavy imaging/torch stack, which application modules must load lazily.
    import mediapipe as mp
    import torch
    from controlnet_aux import OpenposeDetector

    protobuf_version = tuple(int(part) for part in version("protobuf").split(".")[:3])
    if protobuf_version < (5, 29, 6):
        raise RuntimeError(f"protobuf {version('protobuf')} is vulnerable; need >=5.29.6")
    if not hasattr(mp, "solutions"):
        raise RuntimeError("installed MediaPipe no longer provides the required solutions API")
    if OpenposeDetector is None:
        raise RuntimeError("controlnet_aux OpenposeDetector import failed")
    if version("setuptools") != "84.0.0":
        raise RuntimeError(f"expected security-tested setuptools 84.0.0, got {version('setuptools')}")
    if not torch.__version__.startswith("2.11.0"):
        raise RuntimeError(f"expected the pinned torch 2.11.0 runtime, got {torch.__version__}")

    print(
        "optional processor imports OK "
        f"(protobuf {version('protobuf')}, mediapipe {version('mediapipe')}, "
        f"controlnet-aux {version('controlnet-aux')}, setuptools {version('setuptools')}, "
        f"torch {torch.__version__})"
    )


if __name__ == "__main__":
    main()
