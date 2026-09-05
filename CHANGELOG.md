# Changelog

All notable changes to The Wizard's Brush are recorded here. The project follows
[Semantic Versioning](https://semver.org/) and keeps an Unreleased section so
contributors know which user-facing changes need release notes.

## Unreleased

No changes yet.

## [0.1.0] - 2026-09-05

### Added

- Public-release documentation, governance, citation metadata, and repository hygiene.
- A dependency-only installation path with `make install`.
- Model-license inventory and explicit software-versus-weight boundary.
- Safer GitHub dependency and code-scanning automation.
- A minimal, reproducible, checksummed standalone Linux bundle with a prebuilt
  frontend and tag-driven release automation.
- Deterministic CycloneDX SBOMs for the locked Python runtime and frontend build
  dependency graphs.

### Changed

- Fresh installs use a minimal one-checkpoint local profile; optional model slots remain available.
- The Remote GPU worker is provider-neutral and supports both shell and
  interactive-notebook execution from the generated single file.
- Local and development servers bind to localhost unless LAN access is explicitly configured.
- HunyuanVideo is opt-in because its community license has geographic and use restrictions.
- Generation-metadata opt-out covers video sidecars, static sidecar serving,
  and ZIP export manifests while retaining the private database record.
- The browser bundle's third-party notice covers its complete locked production
  JavaScript dependency closure as well as its bundled fonts.

### Security

- Added Host and Origin enforcement, URL-free WebSocket credentials, pre-parser
  request limits, decoded-image limits, and finite Remote GPU queues.
- Prevented framing and rejected cross-site browser requests before protected
  API routes can perform work.
- Bounded every Remote GPU response and media decode, validated image/video
  dimensions and timing, reserved derived-video disk space, and made frame
  interpolation use a constant-size working set.
- Made release verification data-only by default and tightened internal archive
  manifest coverage before any optional runtime smoke test.
- Pinned bootstrap tools, default model revisions, Remote GPU direct packages,
  cloudflared, and post-processing weights to reviewed immutable identities.
- Restricted local adapters to safetensors, hardened secret-file permissions,
  and replaced broad WSL firewall changes with reversible port/subnet rules.
- Removed the dedicated paired remote model/adapter profile and preserved
  repository-provided model safety components during pipeline loading.
- Published the supported-version policy, trust boundaries, security
  invariants, reportability criteria, accepted risks, and known limitations.

[0.1.0]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.0
