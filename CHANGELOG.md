# Changelog

All notable changes to The Wizard's Brush are recorded here. The project follows
[Semantic Versioning](https://semver.org/) and keeps an Unreleased section so
contributors know which user-facing changes need release notes.

## Unreleased

### Added

- Variant Sets: take approved source material, vary it along named axes, and
  run every combination as its own tracked job through the existing operations
  (image edit, img2img, inpaint, outpaint, ControlNet, text-to-image). Recipes
  are reusable definitions; each set keeps an immutable snapshot, per-variant
  state, validation and lineage, survives restarts, retries only failed and
  invalid variants, reruns one variant on purpose, and exports its successful
  outputs under deterministic names with a manifest. Optional ordered stages
  derive new variants from each successful output of the stage before.
- A configurable per-set cap, `VARIANT_MAX_COMBINATIONS` (default 1000), checked
  before anything is queued.
- Named finishing steps behind the existing finishing pipeline: exact resize,
  and optional background removal producing genuine PNG alpha (BiRefNet-lite,
  MIT, downloaded on first use at a pinned commit and checksum).
- Deterministic output validation: readable file, format, exact size, alpha
  required or forbidden, transparent corners and coverage, and safe margins.
- A first-class HTTP API, documented in `docs/api.md`: `POST /api/jobs` queues
  any generator or tool from JSON settings and gallery asset ids, validated
  strictly against the same controls the interface renders; `GET /api/jobs/kinds`
  publishes each kind's inputs and a JSON Schema; `GET /api/jobs/{id}/wait` and
  `GET /api/variant-sets/{id}/wait` long-poll for results;
  `POST /api/assets/import` brings images in with their transparency; and
  `GET /api/assets/{id}/file` serves files to token-authenticated scripts.
  `scripts/api_example.py` is a working client. OpenAPI now describes the
  responses of these routes and of Variant Sets.
- A background matte tool (BiRefNet-lite): cut the subject out to a
  transparent PNG, or save the subject or its surroundings as a reusable mask.
  The Variant Set editor uses it to make an inpaint mask in one click.
- Finishing steps (background removal, exact resize, upscale, face restore, in
  any order) are available to every image generator under Full controls.
- Any generation or variant set can be copied as a runnable API request.
- The Variant Set editor imports a source from disk and picks reference images
  for derived stages; the set page shows each derived variant's input, pages
  long stages, reruns a variant together with its dependents, and can
  duplicate, save as a recipe, or delete a set.

### Changed

- Gallery ZIP export and Variant Set export share one archive writer; the
  gallery export's contents are unchanged.
- Thumbnails of transparent images are laid on neutral grey instead of showing
  the pixels hidden under the transparency.
- Queue-page Retry of a failed Variant Set child retries its variant, so the
  set keeps tracking the new attempt.
- Reloading the app no longer re-announces the last twenty jobs' failures, and
  a Variant Set is announced once when it settles instead of once per failed
  child.
- Viewers show transparency on a checkerboard.

### Fixed

- A job lane restarted in the same process (a second app lifespan) stayed bound
  to its first, closed event loop, so nothing queued afterwards ran.

## [0.1.1] - 2026-09-06

### Changed

- Browser access now uses backend-issued, path-scoped session cookies and
  automatically migrates the previous browser profile setting.
- The API access form remains available before protected settings have loaded,
  including first-time sign-in and token-rotation recovery.
- Wildcard parsing now handles malformed delimiter-heavy prompts in linear time
  while preserving the existing prompt language.

### Security

- Browser session cookies are `HttpOnly`, `SameSite=Strict`, secure over HTTPS,
  and contain purpose-bound credentials instead of the reusable API token.
- API headers remain supported for command-line and custom clients, while
  browser credentials remain absent from URLs and script-readable persistence.

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

[0.1.1]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.1
[0.1.0]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.0
