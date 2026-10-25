# Changelog

All notable changes to The Wizard's Brush are recorded here. The project follows
[Semantic Versioning](https://semver.org/) and keeps an Unreleased section so
contributors know which user-facing changes need release notes.

## Unreleased

## [0.2.0] - 2026-10-25

### Added

- Canvas: layered documents over gallery images, saved as revisions. Layers
  have masks, position, scale, rotation, opacity, visibility, order and a
  blend mode; edits are bounded undo/redo separate from autosave, and a save
  from an old revision is refused instead of overwriting. Flatten renders a
  revision into a new gallery image with the server's renderer, a layer can be
  edited through an ordinary job and come back as a new layer, and a missing
  source stays as a placeholder. Over HTTP (`/api/canvas`) and MCP
  (`create_canvas`, `get_canvas`, `save_canvas`, `list_canvases`,
  `flatten_canvas`, `add_canvas_layer`).
- Saved recipes. A generation or edit setup can be saved under a name with a
  short list of exposed fields, then run from the Recipes page,
  `POST /api/recipes/{key}/run`, or the MCP tools `list_recipes`, `get_recipe`,
  `save_recipe` and `run_recipe`. One JSON Schema serves the form and agents.
  Guidance left at the model's default follows a different model, and a run
  checks settings against the live kind. See `docs/api.md` and `docs/agents.md`.
- Studio presets (`/api/studio/presets`): named, versioned creative setups
  that point at a model and adapters by resource id. Every save is an
  immutable revision made from a stated base revision; a stale save conflicts
  without losing either version, curated presets are never edited in place
  (an edit becomes a custom copy), and archiving keeps every revision readable.
- Hugging Face and Civitai imports (`/api/resource-imports`). Inspecting a
  link pins an exact commit or version and lists every file with its hash and
  whether it can be imported, without downloading; an import is an explicit
  durable task that resumes, checks the published sha256, runs the local
  intake checks, needs an acknowledgement for an unknown licence, and only
  fetches over https from public addresses with tokens kept to their provider.
- A resource catalogue (`/api/resources`). Configured models and the LoRA
  library are recorded by identity (`hf:<repo>@<commit>`, or `local:sha256:`
  for a file), with installs, licence and compatibility observations kept as
  separate facts. Local import uploads in resumable chunks and refuses
  pickle-based formats and archives, validates the safetensors structure,
  refuses a checkpoint offered as a LoRA, deduplicates by content and promotes
  atomically. A LoRA file that disappears stays listed as missing.
- Preset requests through `POST /api/jobs` (`preset_id` or `operation` and
  `model`, with overrides). A dry run returns the execution plan: the composed
  prompt, ordered adapters, each setting with its origin, the effective params
  the job will store and every value the builder normalized, inputs with
  hashes, readiness and three hashes. Submission queues an ordinary job of the
  plan's kind, refuses a plan that is not ready or went stale since its
  preview, rechecks the plan when the job starts and records the runtime that
  ran it. `POST /api/jobs/{id}/replay` replays exactly or refuses, and can run
  the latest preset revision instead.
- Targets and routing (`/api/targets`): capability reports for this machine
  and the Remote GPU, a capability revision (also on `/api/system`), and a
  routing policy that decides whether Auto may use a remote worker and whether
  inputs may be sent there. Plans report readiness as ready, needs resources,
  needs check or blocked; unmeasured hardware is never reported as ready, and
  a planned job's model affinity and lane follow its plan.
- Remote resource preparation (`POST /api/resources/{id}/prepare`): an imported
  adapter is copied to the Remote GPU worker by content, resumed at the
  worker's offset after an interruption, verified by sha256 before it counts,
  and recorded as a remote install. The worker stores it under its hash and
  never takes a path. Applying user adapters in remote jobs is not part of
  this release.
- MCP tools for presets, plans, targets, resources, masks and cancellation:
  `list_targets`, `list_presets` and the preset authoring tools,
  `resolve_preset` (the same hashes as the HTTP dry run), `run_preset`,
  `replay_job`, `get_plan`, `cancel_job` (which tells a requested cancel from a
  settled one), `import_mask`, `fetch_asset` (full-resolution bytes up to
  16 MB) and resource tools. Asset outcomes now name `mime_type`, `warnings`,
  `plan_id` and a video's `duration_s`. `docs/parity-matrix.md` lists what the
  UI, HTTP and MCP each support.

- A preset-first **Studio** is the home page: choose a preset, write the
  subject, see the effective setup (model, adapters, settings with where each
  came from, finishing and readiness) and run exactly the previewed plan. The
  **Preset library** (`/presets`) edits presets with revisions and conflict
  protection; the generator pages moved to **Processes**. On a phone the
  Studio splits into Compose and Results panes, and every failed job shows
  its reason.
- **Resources** (`/resources`): the model and adapter inventory with licence
  (an unknown one is flagged), state, verification and installs; local
  upload in resumable chunks; provider import that inspects a link first and
  installs only an explicit choice; and Prepare on Remote GPU for adapters.
- Settings gains **Routing**: whether the Remote GPU may be used, whether Auto
  may choose it, and whether inputs may be sent to it, with a summary of what
  each target can run.
- **Reference sets** (`/api/reference-sets`, `/references`): named, ordered
  references by role (a character, a product, a style) with per-entry masks
  and notes, saved as revisions. A preset role can bind a set and a request
  can fill a role from one; the plan pins the revision, a set that does not
  fit the role is refused rather than trimmed, and the Studio offers a set
  per role. Reference material only: no model is promised to keep an
  identity.
- Validators can be **required** or **optional**. A required check that cannot
  run (no source, no face, no transparency to measure) or did not run fails;
  an optional failure warns and the variant advances. A preset's validation
  policy carries into its plan and into Variant Set stages.
- **Preset Variant Set stages**: a stage can run a studio preset revision;
  the set pins the revision it resolved and keeps the expansion in its
  snapshot, and explicit stage settings override the preset's.
- **Sprite sheets and atlases** from a set's final stage: equal-cell grids or
  shelf-packed atlases with trimming, padding, edge extrusion, pixel-art
  scaling and several pages, a preview of pages and missing frames before
  anything is written, and a versioned manifest. Exports are validated
  against the written pages and published as **artifacts**
  (`/api/artifacts`) with a SHA-256 and a seven-day download window.
- MCP gains reference-set tools, Variant Set orchestration
  (`preview_variant_set`, `create_variant_set`, `wait_variant_set`,
  `cancel_variant_set`, `retry_variant_set`, `rerun_variant_item`), and sprite
  and artifact tools (`preview_sprite_export`, `create_sprite_export`,
  `get_artifact`, `fetch_artifact`).
- The API describes itself to programs and AI agents: `GET /llms.txt` is a
  plain-text guide addressed to the running server, and `GET /openapi.json`
  declares the `X-API-Token` security scheme and the server's own address, so
  API tools and agent frameworks can import it without editing. `/docs`
  gains an **Authorize** button for the token.

### Changed

- The Remote GPU control keeps polling a running pod, applies only the newest
  answer, and keeps a stop the provider did not confirm on screen as possibly
  billing, with Retry stop and a pod-id-confirmed Forget.

### Not yet qualified

- No curated presets ship: each needs real GPU runs, an evaluation pack and
  human review before it can be called curated (`studio/curated/LOCK.json`
  is empty on purpose).
- Native Windows: the source checkout was smoke-tested on Windows 11 build
  26200 (setup, start, single instance, stop, restart, no orphan processes,
  the backend suite). Local GPU generation on Windows, packaged installers and
  the cross-platform timing comparison are not qualified.
- Applying user adapters in Remote GPU jobs is not part of this release;
  preparing them on the worker is.

### Fixed

- A Runpod stop that is not confirmed no longer forgets the pod. A timeout, a
  refused key or a server error used to clear the only record of hardware that
  could still be billing. The session is now kept as *stop not confirmed* or
  *stop failed* through restarts, Stop checks with Runpod before it repeats the
  termination, Start refuses while the stop is unresolved, and
  `POST /api/remote-gpu/session/forget` drops the record only for the pod id it
  is given. Status polls keep the last provider answer (`observed_at`) and the
  reason the latest one failed.
- A saved recipe runs the prompt Generate sends. Style chips used to be dropped
  on save, so a recipe with the same subject produced a different prompt. The
  selected styles are now frozen with their text (`styles`) and composed onto
  the run's prompt the same way; `POST /api/jobs` accepts the same parts as
  `composition`, and the job keeps the subject and styles apart.
- Ordered finishing steps survive a draft, prompt history and reuse. They used
  to fall back to defaults with a stale-settings warning; a step whose
  processor is no longer offered is now dropped by name and the rest kept.
- Ctrl/Cmd+Enter submits only from the generator's own panel. It used to fire
  under the Save recipe dialog, the asset viewer and pickers.
- A newer source image can no longer be replaced by an older one that finished
  loading later, and replacing the source clears the mask painted on the old
  image at once instead of when the new picture decodes.
- All settings on an asset starts from the job that made it when that job
  still exists. Reuse from image metadata alone says it is partial and names
  what the file did not record.

### Added (native Windows preview)

- `wizards-brush.cmd` sets up, starts, stops, restarts and checks a source
  checkout on native Windows without administrator rights, with every cache
  and profile directory contained in the checkout and uv and Node verified by
  SHA-256. `scripts/serve.py` gives every platform one recorded server, stale
  record recovery after a crash, a graceful stop and, on Windows, a job object
  so no child process outlives the server. `.env` and runtime settings get an
  owner-only access list on Windows. The host-RAM guard reads Windows memory
  instead of reporting it unknown, and `/api/system` shows the probe with its
  reason when it cannot tell. Smoke-tested on Windows 11 build 26200; local GPU
  generation on Windows is not qualified. See `docs/getting-started.md`.

## [0.1.8] - 2026-10-21

### Fixed

- A refused OpenAI image edit no longer saves the upload. The images are
  checked in memory and stored only after the request is accepted. Omitting
  `model` selects `brush-fast`. Copy as an MCP edit includes the generator
  kind, so a local inpaint is not sent as a remote image edit.
- `dry_run` no longer stores anything. An MCP `edit` dry run decodes a data
  URL in memory instead of importing it and starting captioning, and a refine
  dry run validates its mask without writing the mask file. A repeated
  `request_id` on `edit` is recognized before those images are imported. A
  dry run also validates while the Remote GPU worker is offline.
- Named region masks send float32 to their ONNX graphs. The mean and standard
  deviation were Python tuples, which promoted the pixels to float64, and both
  graphs rejected that.
- Remote inpaint keeps its padded crop and the lossless source overlay in the
  app. Asking the worker to crop again could fail after the image had rendered,
  because diffusers resizes the image and not the mask before its own overlay.

### Added

- OpenAI-compatible image endpoints at `/api/openai/v1`: list models, generate,
  and edit. Jobs are the same ones the form and `POST /api/jobs` create.
  See `docs/agents.md`.
- The generator's copy button is a menu: an API request, an MCP call, and an
  OpenAI SDK snippet for image kinds that map.
- An MCP server at `/api/mcp` (Streamable HTTP), plus `scripts/brush-mcp.sh`
  for clients that only speak stdio. Tools list kinds, describe their schema,
  generate, edit, wait, and search the gallery. See `docs/agents.md`.
- Job refusals include `code` and, when known, `field`, `valid_options` and
  short `suggestions`, on both `POST /api/jobs` and the MCP tools. `detail`
  stays a string. `dry_run` validates a generate or edit without queueing it.
- Jobs record the client that submitted them (`ui`, `mcp`, or the API token's
  name). The value is stored on the job, copied into output metadata, and
  returned on job and asset responses. Set `API_TOKEN_NAME` to label token
  clients. `Authorization: Bearer` is accepted alongside `X-API-Token`.

### Changed

- The database records the submitting client (migration `0009`). A backup of
  `output/gen.db` is written before it runs; 0.1.7 will not open the migrated
  database, so keep that backup to roll back.
- Python and frontend dependencies moved forward together, and the worker
  image's lock with them: FastAPI 0.142.4, uvicorn 0.54.0, peft 0.21.2,
  filelock 4.0.12, accelerate 1.15.0, React 19.3.0, Vite 8.3.3, Vitest 5.0.3.
  The frontend lock also takes undici 8.11.2, brace-expansion 5.0.12 and
  source-map-js 1.2.2. diffusers, transformers and torch are unchanged, but
  the lock change gives the worker image a new build id.

## [0.1.7] - 2026-10-11

### Fixed

- Remote image edits no longer fail with CUDA out of memory. The masked-inpaint
  pipeline that shares the edit model's weights was built with
  `DiffusionPipeline.from_pipe`, which casts the shared modules to float32 when
  no dtype is given - a 54 GiB model on its way to 108 GiB, failing 24 GiB in
  and taking every edit with it. It is now constructed from the loaded
  components directly, and only when an inpaint job asks for it.
- The worker's dependency closure is resolved from the app's own lock
  (diffusers 0.40, transformers 5.16, torch 2.11) and a test keeps the two from
  drifting again. The image ran diffusers 0.36, which does not know
  Qwen-Image-Edit-2511's `zero_cond_t` flag, so edits ran without it.
- A worker image is rebuilt when its dependency lock or Dockerfile changes, not
  only when `worker/*.py` does: the build id now covers all three, and the image
  carries them so it fingerprints itself from the same bytes as the checkout.
- Start GPU passes this install's model slots and knobs to the pod as
  environment, so a `.env` naming an alternate image model produces that
  model's picker on the published image instead of configuring nothing.
- Start GPU asks Runpod for a host whose CUDA driver can run the image's torch
  (`RUNPOD_MIN_CUDA`, default 12.8).
- The Diagnosis page no longer tells you to rebuild a single-file runner that
  no longer exists; the remedy for a stale worker is a new pod.

### Added

- The Remote GPU worker accounts for its memory: every model load step logs
  VRAM, an eviction that leaves memory behind says so, and `/health` reports
  VRAM in use plus the torch/diffusers/transformers versions a job runs on. The
  Diagnosis page shows VRAM in use for the remote lane.
- `make remote-gpu-lock` re-resolves the worker's hashed dependency closure
  against the app's pins.

### Known limitation

- HiDream-O1 stays out of the picker on the published image. Its torch floor
  is met now, but its pinned runner fails on transformers 5 (`KeyError:
  'default'` in its rotary-embedding setup, measured on an A100), so the
  worker advertises the slot only on torch >= 2.10 **and** transformers < 5.

## [0.1.6] - 2026-09-26

### Fixed

- The Remote GPU worker advertises HiDream-O1 only when its torch can run it
  (2.10 or newer). The published image ships 2.8, so the app no longer offers
  a model whose every job failed within a second.
- Start GPU with a private or missing published image says there is no public
  image for the build, instead of "could not check ... try again".

## [0.1.5] - 2026-09-26

### Added

- High-fidelity image edits that keep unmasked pixels as the photograph: a
  Remote GPU `inpaint_remote` kind on the same Qwen-Image-Edit-2511 weights,
  `input_fidelity=high` on edit paths (protect-face or a named SCHP region,
  never a full-frame Plus restyle), and Refine on any gallery image — one
  sentence, routed as an ordinary `inpaint_remote` or `image_edit` job, with a
  wand click or a hair / clothes / mouth region.
- Photoreal finishing (no GFPGAN), a native-transparent background option, and
  an A100 Ultra bucket toward a 2K long side. Lightning stays the interactive
  default; High is print.
- Face identity via OpenCV SFace and YuNet (Apache-2.0), an unmasked-pixel
  lock, and a named cutout profile (coverage and safe margin, not four
  transparent corners). Portrait pack and Product cutout ship as Variant Set
  recipes on first start.
- Variant Set previews include an ETA. The remote lane can overlap the next
  submit with local finishing. A restart completes a running job that already
  has assets instead of cancelling it.
- Local inpaint uses the selected family's InpaintPipeline (Klein included).
- The Remote GPU worker is a package, `worker/`, deployed as a container image
  (`make remote-gpu-config`, `make remote-gpu-image`). Config precedence is
  environment, then the baked image config, then defaults; secrets are
  environment only, so an image is publishable.
- Each release publishes that image to
  `ghcr.io/wizards-ecosystem/wizards-brush-remote-gpu`, tagged with the worker
  build id and attested.
- Start GPU: with `REMOTE_GPU_PROVISIONER=runpod` and a Runpod API key, the app
  rents a pod, points the Remote GPU lane at it and terminates it on Stop or a
  clean exit. It shows the hourly rate before starting and the spend while
  running, refuses a second pod, and after a crash names a pod still running at
  startup. `RUNPOD_IMAGE` is optional; empty runs the published image matching
  the checkout, after checking it exists. See `docs/runpod.md`.

### Changed

- The remote lane is labelled "Remote GPU" everywhere a person reads it. The
  stored generator id `colab_a100` is unchanged.

### Removed

- The single-file `remote_gpu.py` worker, `make remote-gpu` and the notebook
  path. Build or pull the container image instead.

### Fixed

- 502/530 from a flaky tunnel is retried only while Remote GPU `/health` is up.
- The region wand maps a click onto the painted image pixels, including when
  the lightbox is letterboxed or zoomed.

## [0.1.4] - 2026-09-18

### Changed

- The Remote GPU worker's refusal to start on a runtime its hashed dependency
  lock does not cover now names the platform, architecture and Python version
  it found. The Remote GPU guide adds that managed runtimes can move to a newer
  default Python, so pin one that ships 3.12.

## [0.1.3] - 2026-09-18

### Changed

- Remote GPU kinds follow the worker's live state. `GET /api/jobs/kinds` marks
  them unavailable, with the reason, while the Remote GPU is not connected.
  `POST /api/jobs` answers them with 503 instead of queuing a job that could
  only fail, after checking the request so a malformed one still gets its 400.
  Variant Set previews warn, and creating a set that needs the offline worker is
  refused, as the editor already does.
- The OpenAPI document lists the error answers each route can give (400, 401,
  404, 409, 503) with their `{"detail": ...}` body.
- API responses no longer reveal where the app is installed. File paths in job
  records, job errors, prompt history and Variant Set item params are shown
  relative to the app folder, and as `~` for the rest of the home directory.
  Stored records are unchanged.

### Fixed

- The API guide's Variant Set naming example: `naming.prefix` is prepended
  exactly as written, so the example now ends it with `/`. The editor explains
  this under the Prefix field.
- The test suite no longer inherits `API_TOKEN` from a developer's `.env`, which
  turned every API test into a 401 on a machine with a token set.

## [0.1.2] - 2026-09-18

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

- The database gains the Variant Set tables (migration `0008`). A backup of
  `output/gen.db` is written before it runs; 0.1.1 will not open the migrated
  database, so keep that backup to roll back.
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

[0.2.0]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.2.0
[0.1.8]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.8
[0.1.7]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.7
[0.1.6]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.6
[0.1.5]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.5
[0.1.4]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.4
[0.1.3]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.3
[0.1.2]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.2
[0.1.1]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.1
[0.1.0]: https://github.com/wizards-ecosystem/wizards-brush/releases/tag/v0.1.0
