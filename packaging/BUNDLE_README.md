# The Wizard's Brush

**Standalone Linux bundle**

A local-first atelier for generative images and video, packaged with its web
interface and reproducible setup path ready to install.

This is the small end-user bundle. It contains the application, a prebuilt web
interface, locked dependency metadata, and the notices required to distribute
it. It deliberately excludes Git history, tests, contributor tooling, frontend
source, package caches, model weights, and generated media.

## Install

Requirements: 64-bit Linux or WSL2, an NVIDIA CUDA GPU with a working driver,
Bash, curl, tar with xz support, `sha256sum`, and `realpath`. Installation and
first model setup require internet access.

```bash
cp .env.example .env
./install.sh --with-models
./start.sh
```

Open <http://127.0.0.1:8000>. The first model load can take several minutes.
Use `./install.sh` without `--with-models` to install the application first and
download the configured models later.

Everything mutable stays in this directory: `.runtime/`, `.venv/`, `models/`,
and `output/`. Back up `output/`, `models/loras/`, and `.env`. To uninstall,
stop the app and remove this extracted directory.

## Verify and learn more

The adjacent `.sha256` file verifies the downloaded archive:

```bash
sha256sum -c wizards-brush-*.tar.xz.sha256
```

The release also provides separate CycloneDX 1.5 SBOMs for the locked Python
runtime and frontend build dependency graphs. They are not required to
run the application, but help downstream users audit their supply chain.

`MANIFEST.sha256` verifies every file inside the extracted bundle. Do not omit
`LICENSE`, `NOTICE`, or `THIRD_PARTY_NOTICES.md` when redistributing it. Model
weights and NVIDIA components are separately licensed; read
`docs/model-licenses.md` before enabling another checkpoint.

Run `./install.sh --check` at any time before installation to validate both the
archive contents and required host commands without downloading anything.

- Documentation: <https://github.com/wizards-ecosystem/wizards-brush/tree/main/docs>
- Issues: <https://github.com/wizards-ecosystem/wizards-brush/issues>
- Security: <https://github.com/wizards-ecosystem/wizards-brush/security/policy>
- Source: <https://github.com/wizards-ecosystem/wizards-brush>
