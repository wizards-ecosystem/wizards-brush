# The Wizard's Brush — Remote GPU worker, as a container.
#
# Why this exists: the worker was written for a notebook or a bare VM, where the
# only way to get dependencies is to install them at startup. On rented GPU
# hardware that is the worst possible moment — a network call, a dependency
# resolver and an ABI negotiation, each able to fail minutes after the clock
# started, on a machine you are paying for by the second. This image does all of
# it once, at build time, on your own machine.
#
# It also removes the Python-3.12 gamble entirely. The worker refuses any other
# interpreter because its lock is a set of cp312 manylinux wheel hashes, and this
# base *is* 3.12 — not a host image that might be.
#
# Build (from the repo root, after `make remote-gpu-config`):
#   make remote-gpu-image
#
# --platform=linux/amd64 matters: Runpod hosts are x86_64, and an arm64 image
# built on Apple Silicon fails with "exec format error" after the push, not
# before it.
#
# Never tag this `latest`. Hosts cache images per machine, so a mutable tag
# serves stale code with no way to tell which build is live — the same problem
# /health's `build` fingerprint exists to catch. Tag with the build id.

# ---------------------------------------------------------------------------
# Base: a plain Python image, NOT nvidia/cuda.
#
# Measured on the cuda-base build: `cuda-libraries-12-8` 3.11 GB + cuDNN 1.05 GB
# + cudart 204 MB, while the torch layer was 6.96 GB — because torch's cu128
# wheels already pull nvidia-cublas/cudnn/cufft/... as dependencies and ship
# their own CUDA runtime. The base was carrying a second copy of libraries torch
# brings anyway, for ~4.4 GB.
#
# What the container genuinely needs from outside is the *driver*, and that comes
# from the host through the container runtime, not from any image. So the CUDA
# base buys nothing here.
#
# If you ever add something that links system CUDA (a compiled extension, nvcc),
# this is the line to change back — and the image will grow by those 4.4 GB again.
# ---------------------------------------------------------------------------
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# torch, torchvision and numpy are deliberately NOT in the hashed lock: the lock
# is the non-Torch closure, installed with --no-deps so it can never quietly
# replace a working CUDA build. They are pinned here instead, where the CUDA
# version they must match is also chosen.
#
# The torch version is the app's (pyproject.toml); only the CUDA build differs,
# because Runpod's A100 hosts report CUDA 12.8 while the local venv is cu130.
# 2.10 is also the floor for HiDream-O1's runner - on 2.8 the worker had to
# leave that slot out of /health.
ARG TORCH_VERSION=2.11.0
ARG TORCHVISION_VERSION=0.26.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# git: _prepare_hidream() pins the HiDream runner by commit.
# libgomp1: torch's OpenMP runtime, which a slim base does not carry and which
# fails at import rather than at build time if it is missing.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

RUN python -m pip install --index-url "${TORCH_INDEX_URL}" \
        "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}"

# The same hashed, binary-only, --no-deps install the worker used to do at
# runtime — run here instead, and only once. The lock stays in the image, inside
# the package: `worker.build_id()` hashes it along with the sources and this
# Dockerfile, so an image can tell which build it is from its own bytes.
# `pip check` closes the gap --no-deps opens: a closure missing a package would
# otherwise surface as an ImportError on a rented GPU.
WORKDIR /app
COPY scripts/remote-gpu-requirements.lock /app/worker/requirements.lock
RUN python -m pip install --require-hashes --only-binary=:all: --no-deps \
        -r /app/worker/requirements.lock \
    && python -m pip check

# Some CUDA bases ship torchao 0.10, which peft's LoRA loader rejects. We run
# full bf16 and never use it.
RUN python -m pip uninstall -y torchao || true

# The worker package. It carries no secrets by construction: HF_TOKEN and
# REMOTE_GPU_SHARED_SECRET arrive as deploy-time environment, so this image is
# publishable as-is.
COPY worker /app/worker
COPY docker/remote-gpu/Dockerfile /app/worker/Dockerfile
# Non-secret build config: model slots + reviewed revision pins.
# Generate with `make remote-gpu-config` before building.
COPY docker/remote-gpu/remote-gpu-config.json /app/remote-gpu-config.json

ENV REMOTE_GPU_CONFIG_FILE=/app/remote-gpu-config.json \
    PORT=8000

# Prove the image can import what it will run, here on the build machine rather
# than on a metered GPU. The placeholder secret only satisfies config's refusal
# to start without one; it is not baked in (RUN environment is not image ENV).
RUN REMOTE_GPU_SHARED_SECRET=image-build-check python -c \
        "import worker.api, diffusers, transformers, peft, accelerate, imageio_ffmpeg, sentencepiece; \
         from worker import build_id; print('build', build_id())"

# Weights, caches and the HiDream checkout all live under REMOTE_GPU_ROOT.
# Leaving it inside the container means re-downloading tens of GB every start,
# so point it at persistent storage at deploy time:
#   pod:        -e REMOTE_GPU_ROOT=/workspace/wizards-brush
#   serverless: -e REMOTE_GPU_ROOT=/runpod-volume/wizards-brush
# The in-image default is correct only for a throwaway smoke test.
ENV REMOTE_GPU_ROOT=/app/.wizards-brush-remote-gpu

EXPOSE 8000
CMD ["python", "-m", "worker"]
