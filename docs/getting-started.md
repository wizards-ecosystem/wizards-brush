# Getting started

The Wizard's Brush keeps its interpreter, Node runtime, packages, caches,
models, database, and generated media inside the checkout. You do not need a
system Python or Node installation, and deleting the checkout removes the app.

## Fastest path: standalone release

The release page provides a small Linux x86-64 runtime archive with the web
interface already built. It does not require Git, Make, Node, tests, or frontend
source. Download its `.tar.xz` and adjacent `.sha256` file, then:

```bash
sha256sum -c wizards-brush-*.tar.xz.sha256
tar -xJf wizards-brush-*.tar.xz
cd wizards-brush-*-linux-x86_64
cp .env.example .env
./install.sh --with-models
./start.sh
```

Use the source-checkout path below when contributing or changing the frontend.
See [Distribution](distribution.md) for the exact artifact contents, provenance,
upgrade, and rollback contract.

## Requirements

The supported local-generation path is Linux or WSL2 on x86-64 with an NVIDIA
CUDA GPU. The starter profile targets a 16 GB card. Smaller cards may work with
reduced dimensions and offload, but are not the release-tested baseline.

Have these host tools available:

- Git, GNU Make, Bash, curl, tar with xz support, `sha256sum`, and `realpath`.
- A working NVIDIA driver visible through `nvidia-smi`.
- At least 40 GB free for the starter model and dependencies. Allow roughly
  100 GB if you enable the optional local quality and comparison models.
- Enough host RAM for model loading and CPU offload. The app checks before a
  large load and reports a useful refusal instead of risking a machine-wide OOM.

macOS, native Windows, AMD ROCm, Apple Metal, and CPU-only generation are not
currently supported. A machine without a local NVIDIA GPU can run the web/API
shell and connect to an operator-controlled Remote GPU, but that is an advanced
deployment rather than the beginner path.

## Install

```bash
git clone https://github.com/wizards-ecosystem/wizards-brush.git
cd wizards-brush
cp .env.example .env
make install
```

`make install` downloads pinned project-owned versions of Python, uv, and Node;
installs locked dependencies; and builds the frontend. It does not download
model weights. Review `.env`, then install the enabled starter profile:

```bash
make models
make doctor
make start
```

Open <http://127.0.0.1:8000>. The first model load can take several minutes even
after the model is cached. Open **Diagnosis** to see the active hardware profile,
model readiness, and any actionable warning.

For a single command after reviewing `.env`, `make setup` performs both
`make install` and `make models`.

## Configuration choices before downloading

The default profile enables one Apache-2.0 local checkpoint,
`Tongyi-MAI/Z-Image-Turbo`. Optional model slots stay empty so a clean install
does not silently fetch an evaluation suite. Add model IDs to `.env` before
`make models` if you want those choices installed eagerly.

`HF_TOKEN` is only required for gated or private Hugging Face repositories. The
starter model is ungated. Read [model licenses](model-licenses.md) before adding
or enabling any checkpoint; model terms are independent from the application's
Apache-2.0 license.

## Localhost and LAN access

The safe default is `HOST=127.0.0.1`, so only this machine can reach the app.
For access from a trusted LAN, set both values in `.env`:

```dotenv
HOST=0.0.0.0
API_TOKEN=replace-with-a-long-random-value
```

Restart with `make start`, then open `http://<machine-ip>:8000` from the other
device and enter the same API token in **Settings -> API access**. Startup
refuses this non-loopback listener if the token is empty. You may also need an
operating-system firewall rule. Do not port-forward this service to the public
internet. Follow the [network security guide](network-security.md) for HTTPS,
exact browser origins, and the reversible WSL2 firewall helper.

## Development

```bash
make dev
make browser-dev
```

The backend runs on port 8000 with reload and Vite runs on port 5173. The second
command opens a browser profile contained under `.runtime/`. Run `make check`
before proposing a change.

## Updating

Stop the app and back up `output/` before a significant update. Then:

```bash
git pull --ff-only
make install
make start
```

Database migrations run at startup and create a backup before the first
unapplied migration. Run `make models` only when the model configuration or
download requirements changed.

## Backing up your work

Back up these paths while the app is stopped:

- `output/` - SQLite database, generated media, thumbnails, uploads, and runtime settings.
- `wildcards/` - editable prompt lists.
- `models/loras/` - user-supplied adapters and their sidecars.
- `.env` - local configuration and secrets; store it in a private secret manager or encrypted backup.

The model cache under `models/huggingface/` is large but reproducible and can be
downloaded again.

## Troubleshooting

- **A command is missing:** install the host tools listed above, then rerun the command.
- **`nvidia-smi` fails:** repair the host or WSL NVIDIA driver before debugging the app.
- **A model download is unauthorized:** accept the model's upstream terms when required and set a valid `HF_TOKEN`.
- **A model download is refused for disk:** free space inside the checkout's filesystem; the guard preserves a safety reserve.
- **The page says the frontend is not built:** run `make install` or `make build`.
- **The API port is busy:** stop the other process or change `PORT` in `.env`.
- **A second app instance refuses to start:** one process owns the SQLite queue by design; use `make stop` first.
- **ControlNet refuses to run:** follow the `LOCAL_OFFLOAD` guidance in `.env`; the unsafe placement combination is blocked deliberately.
- **You want Nunchaku:** it is deliberately not auto-installed. Obtain a wheel
  matching the exact Python/torch/CUDA stack from its official release, verify
  the publisher's digest, then run `make install-nunchaku` with
  `NUNCHAKU_WHEEL_URL` and `NUNCHAKU_WHEEL_SHA256` set.

If the issue remains, follow [SUPPORT.md](../SUPPORT.md) and include the
non-secret Diagnosis report.

## Cleaning and removal

`make clean-build` removes rebuildable frontend output. `make clean-cache`
removes project caches while preserving models and generated work.
`make clean-runtime` removes the local toolchain, environment, Node packages,
and build output; rerun `make install` to recreate them.

To uninstall completely, run `make stop`, preserve any backup you want, and
remove the checkout. No global Python or Node package was installed by the
project setup path.
