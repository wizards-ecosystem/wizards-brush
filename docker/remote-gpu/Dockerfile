# The Wizard's Brush — Remote GPU worker, as a container.
#
# Why this exists: the worker was written for a notebook or a bare VM, where
# the only way to get dependencies is to install them at startup. On rented GPU
# hardware that is the worst possible time — a network call, a dependency
# resolver and an ABI negotiation, each able to fail minutes after the clock
# started, on a machine you are paying for by the second. This image does all of
# it once, at build time, on your own machine.
#
# It also removes the Python-3.12 gamble. The worker refuses any other
# interpreter because its lock is a set of cp312 manylinux wheel hashes; a stock
# host image may or may not oblige. Here we choose the base, so it always does.
#
# Build (from the repo root, after `make remote-gpu-config`):
#   docker build --platform=linux/amd64 -f docker/remote-gpu/Dockerfile -t wizards-brush-remote-gpu:<tag> .
#
# --platform matters: Runpod hosts are x86_64, and an arm64 image built on Apple
# Silicon fails with "exec format error" after the push, not before it.
#
# Never tag this `latest`. Hosts cache images per machine, so a mutable tag
# serves stale code with no way to tell which build is live — the same problem
# /health's `build` fingerprint exists to catch. Tag with the build id.

# CUDA 12.8 covers A100 / H100 / H200. For an RTX PRO 6000 (Blackwell, sm_120)
# pass a CUDA 13 base and a matching torch index: Runpod reports no 12.8 hosts
# for that card at all.
ARG CUDA_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04
FROM ${CUDA_IMAGE}

# torch, torchvision and numpy are deliberately NOT in the hashed lock: the lock
# is the non-Torch closure, installed with --no-deps so it can never quietly
# replace a working CUDA build. They are pinned here instead, where the CUDA
# version they must match is also chosen.
ARG TORCH_VERSION=2.8.0
ARG TORCHVISION_VERSION=0.23.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# git is not optional: _prepare_hidream() pins the HiDream runner by commit.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# A venv, not the system interpreter. Ubuntu 24.04 marks its Python
# externally-managed (PEP 668) and ships Debian-packaged libraries pip cannot
# uninstall — the documented cause of "Cannot uninstall cryptography ... no
# RECORD file was found" on images like this one.
RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN python -m pip install --upgrade pip setuptools wheel

RUN python -m pip install --index-url "${TORCH_INDEX_URL}" \
        "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}"

# The same hashed, binary-only, --no-deps install the worker used to do at
# runtime — run here instead, and only once.
COPY scripts/remote-gpu-requirements.lock /tmp/remote-gpu-requirements.lock
RUN python -m pip install --require-hashes --only-binary=:all: --no-deps \
        -r /tmp/remote-gpu-requirements.lock \
    && rm /tmp/remote-gpu-requirements.lock

# Some CUDA bases ship torchao 0.10, which peft's LoRA loader rejects. We run
# full bf16 and never use it.
RUN python -m pip uninstall -y torchao || true

WORKDIR /app
# The worker package. It carries no secrets by construction: HF_TOKEN and
# REMOTE_GPU_SHARED_SECRET arrive as deploy-time environment, so this image is
# publishable as-is.
COPY worker /app/worker
# Non-secret build config: model slots + reviewed revision pins + build id.
# Generate with `make remote-gpu-config` before building.
COPY docker/remote-gpu/remote-gpu-config.json /app/remote-gpu-config.json

ENV REMOTE_GPU_CONFIG_FILE=/app/remote-gpu-config.json \
    PORT=8000

# Weights, caches and the HiDream checkout all live under REMOTE_GPU_ROOT.
# Leaving it inside the container means re-downloading tens of GB every start,
# so point it at persistent storage at deploy time:
#   pod:        -e REMOTE_GPU_ROOT=/workspace/wizards-brush
#   serverless: -e REMOTE_GPU_ROOT=/runpod-volume/wizards-brush
# The in-image default is correct only for a throwaway smoke test.
ENV REMOTE_GPU_ROOT=/app/.wizards-brush-remote-gpu

EXPOSE 8000
CMD ["python", "-m", "worker"]
