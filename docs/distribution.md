# Distribution

The project publishes two intentionally different downloads for each version:

- GitHub's source archives contain the complete tagged FOSS project, including
  tests, frontend source, contributor documentation, and build tooling.
- `wizards-brush-VERSION-linux-x86_64.tar.xz` is the lean end-user bundle. It
  contains only the Python application, prebuilt frontend, runtime installer,
  locked dependency graph, starter configuration, runtime prompt lists, and
  the legal/user documents needed to operate and redistribute it.
- Adjacent Python-runtime and frontend-build CycloneDX 1.5 SBOMs describe the
  exact locked Python graph and the installed Linux frontend build graph. Each
  SBOM has its own `.sha256` file.

The standalone bundle does not contain Git history, Node, frontend source,
tests, model weights, package caches, generated media, databases, credentials,
or a Python environment. The installer downloads the pinned project-owned
Python toolchain and locked runtime dependencies after extraction. Model
downloads remain a separate, visible step because the starter profile is about
32 GB and model licences are independent of Apache-2.0.

`LICENSE`, `NOTICE`, and `THIRD_PARTY_NOTICES.md` are deliberately present in
the lean bundle. They are small, legally meaningful runtime distribution files,
not development clutter, and must remain with redistributed copies.

## Install from a release

Download the archive and its `.sha256` file from the same GitHub release. The
SBOMs are optional for installation but useful for policy and vulnerability
audits:

```bash
sha256sum -c wizards-brush-*.tar.xz.sha256
tar -xJf wizards-brush-*.tar.xz
cd wizards-brush-*-linux-x86_64
cp .env.example .env
./install.sh --with-models
./start.sh
```

The archive's `MANIFEST.sha256` covers every internal file. Its
`RELEASE-MANIFEST.json` records the source commit, version, target, build epoch,
and install command. Official tag builds also receive a GitHub artifact
attestation, which can be verified with the GitHub CLI.

```bash
gh attestation verify wizards-brush-*.tar.xz \
  --repo wizards-ecosystem/wizards-brush
```

## Reproducible maintainer build

The explicit allowlist lives in `packaging/release-files.txt`. The builder
rejects untracked inputs, development/runtime paths, model formats, databases,
bytecode, symlinks, a dirty release tree, mismatched versions, and mismatched
tags. It normalizes archive order, ownership, permissions, and timestamps.

```bash
make release-check
make release-bundle
(cd release && sha256sum -c wizards-brush-*.sha256)
.venv/bin/python scripts/build_release.py \
  --verify release/wizards-brush-*.tar.xz
# Only after validating the published checksum or GitHub attestation:
.venv/bin/python scripts/build_release.py \
  --verify release/wizards-brush-*.tar.xz --runtime-smoke
```

Plain `--verify` is a data-only structure and checksum check: it never runs code
from the archive. `--runtime-smoke` executes the extracted installer and API, so
use it only after authenticating the outer archive.

For a reproducibility comparison, build the same clean commit twice with the
same `SOURCE_DATE_EPOCH` and compare archive SHA-256 values. `--allow-dirty`
exists only for local development tests and marks the internal manifest; never
publish such an artifact. The SBOM generator likewise normalizes timestamps and
identifiers to the source commit so repeated builds are byte-for-byte stable.

## Upgrade and rollback

Extract a new version beside the old directory, run its installer, stop the old
process, then copy only `.env`, `output/`, `models/`, and any edited
`wildcards/` into the new directory. Start the new version and verify Diagnosis
before deleting the old one. Keeping the previous extracted directory gives a
simple rollback path; always back up `output/` before crossing a database
migration.
