<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brush-logo-dark.svg">
    <img src="docs/assets/brush-logo.svg" alt="The Wizard's Brush" width="420">
  </picture>
</h1>

<p align="center">
  <strong>A local-first atelier for generative images and video.</strong><br>
  Create on your own GPU, shape every detail, and keep every result understandable.
</p>

<p align="center">
  <a href="https://github.com/wizards-ecosystem/wizards-brush/actions/workflows/ci.yml"><img alt="Build status" src="https://img.shields.io/github/actions/workflow/status/wizards-ecosystem/wizards-brush/ci.yml?branch=main&amp;style=flat-square&amp;label=build&amp;color=35633b"></a>
  <a href="https://github.com/wizards-ecosystem/wizards-brush/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/wizards-ecosystem/wizards-brush?display_name=tag&amp;sort=semver&amp;style=flat-square&amp;color=994d34"></a>
  <a href="LICENSE"><img alt="Apache License 2.0" src="https://img.shields.io/badge/license-Apache--2.0-272522?style=flat-square"></a>
  <a href="CONTRIBUTING.md"><img alt="Contributions welcome" src="https://img.shields.io/badge/contributions-welcome-85621d?style=flat-square"></a>
</p>

<p align="center">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white">
  <img alt="Node.js 22" src="https://img.shields.io/badge/Node.js-22-339933?style=flat-square&amp;logo=nodedotjs&amp;logoColor=white">
  <img alt="NVIDIA CUDA" src="https://img.shields.io/badge/GPU-NVIDIA%20CUDA-76B900?style=flat-square&amp;logo=nvidia&amp;logoColor=white">
  <img alt="Linux and WSL2" src="https://img.shields.io/badge/platform-Linux%20%7C%20WSL2-a5a69d?style=flat-square&amp;logo=linux&amp;logoColor=white">
  <img alt="Local-first" src="https://img.shields.io/badge/design-local--first-285e63?style=flat-square">
</p>

<p align="center">
  <a href="#quick-start">Install</a> ·
  <a href="#the-whole-creative-loop">Features</a> ·
  <a href="#from-prompt-to-library">Workflow</a> ·
  <a href="docs/user-guide.md">User guide</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="docs/remote-gpu.md">Remote GPU</a> ·
  <a href="CONTRIBUTING.md">Contribute</a>
</p>

> From the first prompt to the final archive, The Wizard's Brush keeps creation,
> editing, queueing, comparison, and provenance in one coherent studio.

The Wizard's Brush is more than a prompt box wrapped around a model. It brings
model-aware controls, durable queues, live previews, editing and finishing,
searchable assets, reusable recipes, and portable exports into one private
workspace. The interface starts simple and reveals its full control surface
only when you want it.

> [!IMPORTANT]
> The application source is FOSS under Apache-2.0. Model weights, adapters, the
> NVIDIA driver, and CUDA runtime components have separate terms. Read the
> [model and weight license inventory](docs/model-licenses.md) before use.

## The whole creative loop

| | Built for |
| --- | --- |
| **Create and transform** | Text-to-image, img2img, inpaint, outpaint, ControlNet, instruction editing, image-to-video, text-to-video, and finishing |
| **Use the right controls** | Each generator publishes its real capabilities, defaults, and limits; the interface adapts to the selected model |
| **Keep work moving** | Durable queues, live previews, restart recovery, cancellation, and independent local and remote lanes |
| **Build a real library** | Search, captions, tags, collections, favorites, ratings, comparison, soft delete, and portable export |
| **Reproduce with confidence** | Seeds, effective parameters, adapters, model identity, lineage, and finishing operations stay with each asset |
| **Own the workspace** | Localhost defaults, project-contained storage, optional API authentication, and metadata controls |

Finishing uses tiled Real-ESRGAN upscaling and GFPGAN face restoration, with
optional auto-detailing and RIFE frame interpolation. These are explicit,
recorded steps rather than invisible changes to the generated file.

## Quick start

### Requirements

- Linux or WSL2 on x86-64.
- An NVIDIA CUDA GPU; 16 GB VRAM is the release-tested local baseline.
- Git, GNU Make, Bash, curl, tar/xz, `sha256sum`, and `realpath`.
- About 40 GB free for the starter profile. Optional local models can raise the
  cache requirement above 90 GB.

Python and Node are not host prerequisites. The installer downloads pinned
copies under `.runtime/` and keeps package caches inside the checkout.

### Standalone release - recommended

Download the `linux-x86_64.tar.xz` asset and its adjacent checksum from the
[latest release](https://github.com/wizards-ecosystem/wizards-brush/releases/latest),
verify and extract them, then run:

```bash
sha256sum -c wizards-brush-*.tar.xz.sha256
tar -xJf wizards-brush-*.tar.xz
cd wizards-brush-*-linux-x86_64
cp .env.example .env
./install.sh --with-models
./start.sh
```

The standalone archive contains a prebuilt frontend, so it needs neither Git,
Make, Node, nor the repository's tests and contributor files.

### Source checkout

Use the source path when contributing, changing the frontend, or following the
latest development branch:

```bash
git clone https://github.com/wizards-ecosystem/wizards-brush.git
cd wizards-brush
cp .env.example .env
make install     # tools, locked dependencies, frontend; no model weights
make models      # the enabled starter model + curated adapters
make start
```

Open <http://127.0.0.1:8000>, then visit **Diagnosis** before the first large
render. A first load can take several minutes even after the model is cached.

> [!TIP]
> After reviewing `.env`, `make setup` combines `make install` and `make models`.
> The starter model is ungated, so `HF_TOKEN` can remain empty unless you choose
> a gated or private repository.

See the complete [getting-started guide](docs/getting-started.md) for WSL,
trusted-LAN access, backups, updates, troubleshooting, cleaning, and removal.
For any listener beyond localhost, read [network security](docs/network-security.md)
before changing `HOST`.

## From prompt to library

```text
describe or supply media
          |
          v
 choose a process --> Simple controls --> durable local/remote queue
                                                |
                                                v
                                   preview --> result --> gallery
                                                            |
                         reuse settings <--> compare <--> export
```

The Simple form is the default and always includes a prompt. Full controls adds
model choice, schedulers, guidance, adapters, masks, deterministic seeds,
wildcards, X/Y grids, and explicit finishing steps when the selected generator
can actually use them.

## Local and remote compute

The local lane ships with an intentionally small public profile:

- **Z-Image-Turbo** enabled as the Apache-2.0 starter checkpoint.
- Z-Image base, FLUX.2 Klein 4B, SDXL, Chroma, and FLUX.1 slots available as
  explicit `.env` opt-ins.
- One resident pipeline at a time, with model-aware quantization, CPU offload,
  VRAM guards, and visible swap/load stages.

The optional Remote GPU lane supports large image, editing, and video models on
a machine you own or are explicitly authorized to expose. It is not a hosted
service. The generated `remote_gpu_filled.py` contains secrets, stays ignored,
and reports a build fingerprint so stale workers are visible.

The generated worker is provider-neutral and can run from a shell or an
interactive notebook on a compatible Linux GPU runtime. Read
[the Remote GPU guide](docs/remote-gpu.md) before deploying it.

## Your work stays yours

```text
.runtime/              pinned tools, caches, temp, contained browser profile
.venv/                 project Python environment
frontend/node_modules/ locked frontend dependencies
models/                 downloaded checkpoints, tool weights, local LoRAs
output/                 SQLite, generated media, thumbnails, uploads, settings
wildcards/              editable prompt lists
.env                    local configuration and secrets
```

`make doctor` proves that every active writable path resolves inside the
checkout. Back up `output/`, `wildcards/`, `models/loras/`, and the private
`.env`; the rest can be recreated. See the [user guide](docs/user-guide.md) for
queue, gallery, prompt, adapter, finishing, and export behavior.

## Shape the studio

Every supported setting is explained in [.env.example](.env.example). Common
groups are:

| Settings | Purpose |
| --- | --- |
| `LOCAL_IMAGE_MODEL*`, `LOCAL_QUANT`, `LOCAL_OFFLOAD` | Local model slots and memory strategy |
| `A100_IMAGE_MODEL*`, `QWEN_EDIT_MODEL`, `VIDEO_MODEL` | Optional Remote GPU model catalogue |
| `REMOTE_GPU_BASE_URL`, `REMOTE_GPU_SHARED_SECRET` | Remote endpoint and authentication |
| `HOST`, `PORT`, `API_TOKEN`, `CORS_ORIGINS` | Browser/API exposure |
| `EMBED_METADATA`, `EMBED_PROVENANCE` | Replay metadata and AI-origin declaration |
| `OUTPUT_DIR`, `HF_HOME`, `LORA_DIR`, `RUNTIME_DIR` | Project-contained storage paths |

Fresh installs bind to `127.0.0.1`. To use a trusted LAN, set `HOST=0.0.0.0`
and a long `API_TOKEN`, restart, and enter the token in the browser's Settings
page. Do not expose the bare development or application server to the internet.
Startup refuses a non-loopback host without that token; the network guide covers
TLS termination and the narrowly scoped WSL firewall helper.

## Build, run, and verify

| Command | Purpose |
| --- | --- |
| `make install` | Install locked tools/dependencies and build; skip model weights |
| `make setup` | Install everything, including enabled local models |
| `make models` / `make models-status` | Download or audit the configured model profile |
| `make install-nunchaku` | Install an explicitly URL- and SHA-pinned native wheel (advanced) |
| `make start` / `make stop` | Serve the built app on the configured host and port |
| `make dev` / `make browser-dev` | Run reload servers / open the contained browser profile |
| `make doctor` | Verify the project-contained runtime boundary |
| `make check` | Backend lint, types, tests plus frontend lint, format, tests, build |
| `make release-check` | Full local release gate including containment and dependency checks |
| `make release-bundle` | Build and verify the minimal checksummed Linux archive |
| `make remote-gpu` | Generate the ignored operator worker from `.env` |
| `make clean-build` / `make clean-cache` / `make clean-runtime` | Remove increasingly broad rebuildable state |

Run `make help` for the complete list.

## Open source, with clear boundaries

The application is licensed under the [Apache License 2.0](LICENSE). The
[NOTICE](NOTICE) records permissively reused material and the clean-room
boundary around GPL/AGPL projects that were studied but never copied.
[Third-party notices](THIRD_PARTY_NOTICES.md) cover bundled browser libraries
and fonts, while release SBOMs inventory both locked dependency graphs. The
[exploration ledger](docs/exploration-mining-ledger.md) preserves the reasoning
behind upstream-derived ideas.

Before contributing, read [CONTRIBUTING.md](CONTRIBUTING.md),
[GOVERNANCE.md](GOVERNANCE.md), and the [Code of Conduct](CODE_OF_CONDUCT.md).
Ordinary problems follow [SUPPORT.md](SUPPORT.md); vulnerabilities follow
[SECURITY.md](SECURITY.md) and must not be posted publicly.

The project is pre-1.0. Review [CHANGELOG.md](CHANGELOG.md) for release-facing
changes, [the distribution design](docs/distribution.md) for artifact contents,
and [the release checklist](docs/release-checklist.md) for the maintainer gate.
