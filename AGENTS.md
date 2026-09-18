# AGENTS.md

## Commands

- `make dev` - backend :8000 (uvicorn --reload) + Vite :5173; `make start` - single-port serve
- `make bootstrap` installs pinned uv + Node under `.runtime/tools`; setup, build,
  test, and runtime targets source `scripts/project-env.sh`, which makes the
  checkout the HOME/cache/temp boundary
- `make doctor` must show every active writable path inside the checkout
- `make test` - backend pytest (`.venv/bin/python -m pytest tests -q`)
- single test: `.venv/bin/python -m pytest tests/test_db.py -k name -q`
- `make lint` - ruff over `backend tests scripts remote_gpu.py`. The rule set is pinned
  explicitly in `pyproject.toml` (`[tool.ruff.lint] select`) - relying on ruff's defaults
  turned the tree red on a version bump alone. `E402` stays ignored (lazy heavy imports);
  ruff's version comes from `uv.lock`, so CI and local cannot disagree
- `make typecheck` - mypy (pydantic plugin; **includes `remote_gpu.py`** - it is the most
  failure-prone file here, and type-checking it immediately found a Pillow API misuse)
- frontend: `source scripts/project-env.sh && cd frontend && npm run lint && npm run test && npm run build`
  (build runs strict `tsc -b`)
- `make remote-gpu` - inject `.env` secrets into gitignored `remote_gpu_filled.py` for an operator-controlled Remote GPU worker

## Architecture map

- `backend/app/main.py` - app factory, lifespan (init_db → reconcile_orphans → lanes →
  **resume_queued** → warm/sweep thread). `reconcile_orphans` cancels only *running* jobs  -
  their handler died with the old process. Jobs still **queued** never started, so they
  survive the restart and `resume_queued()` puts them back on their lanes by
  `get_handler(kind)`; a kind with no registered handler is canceled with a reason rather
  than left pending forever. Cancelling the whole queue on restart used to throw away work
  the user had deliberately lined up, opt-in API_TOKEN middleware, per-subdir static mounts (never the output root - gen.db/runtime_settings.json must stay unreachable)
- `backend/app/config.py` - pydantic-settings from root `.env`; mutable paths are
  confined to the repo; runtime overrides in `output/runtime_settings.json` beat `.env`
  for the Remote GPU URL/secret
- `backend/app/db.py` - SQLite via SQLModel; schema changes go in `backend/app/migrations/`
  as a new **append-only** module registered in `MIGRATIONS` (SQLModel never ALTERs an
  existing table). Never reorder or edit one that has shipped; an id the build does not
  know is fatal, and a backup is taken before the first unapplied one
- `backend/app/queue.py` - two lanes: `local` (DB-priority-ordered, serialized) and `remote` (Remote GPU, network-bound); `GPU_LOCK` in `generators/base.py` serializes ALL local GPU ops; cooperative cancel + skip via sets checked in `progress_cb`; `progress_cb(frac, msg, preview=...)` takes raw JPEG **bytes** and fans them out as a binary WebSocket frame only (never the DB, never base64 - see `app/wsframe.py`, which must stay in step with `frontend/src/lib/wsframe.ts`)
- `backend/app/generators/registry.py` - **single source of truth for UI controls**; the frontend renders `GET /api/models` dynamically - never hardcode generator controls in React. New generator = registry entry + `JobKind` + handler + route + `register_handler()` (that's what makes rerun work) + `_LOCAL_KINDS` if it runs locally. Controls carry `tier`: `"basic"` ones make up the **Simple form** (the default), everything else appears only under "Full controls". Hiding a control never changes the request - its default still ships - so every non-basic control must have a sane `default`, and a test asserts every generator's Simple form at least has a prompt
- `backend/app/generators/variants.py` - **the model catalogue for BOTH lanes**, keyed by
  `lane` ("local" | the legacy internal key `"colab"`): which models each `model_variant` picker offers and each one's
  step tier + CFG. Local = Z-Image Turbo/base, an SDXL slot, Chroma, FLUX.1-dev; Remote GPU =
  a primary model and a second `alt` slot. Slots whose `.env` setting is empty are simply
  not offered, so the shipped defaults stay minimal and each install configures its own.
  **Every lookup is lane-scoped** - the
  lanes deliberately share the name "quality", and an unscoped `get()` would send a job to
  the other device's model. `resolve_model`, the registry's options and guidance `defaults_by`, and
  `_common_params` all read it, so adding a model is one row rather than four edits that
  drift. A variant is offered only when its `.env` setting names a repo. **Variant names are
  persisted in job params and replayed on rerun - they are API, never rename one.**
- `backend/app/generators/local_image.py` - model-agnostic local pipeline (resolves classes
  from model_index.json), keyed by model id. Only ONE model is resident: `_get()` clears
  `_STATE` and reports a "swapping" stage before loading a different one. Quantization comes
  from `generators/quant.py`; LoRA adapters are applied per generation and **always reset
  first**, since `_pipe_for` hands out pipelines backed by shared components
- `backend/app/generators/quant.py` - `LOCAL_QUANT` backends: `nunchaku` (SVDQuant INT4,
  behind a **subprocess preflight** - a kernel mismatch SIGABRTs and cannot be caught
  in-process), `4bit`/`fp8` (bitsandbytes), `none`. **Measured 2026-08-29: the installed
  nunchaku wheel fails its preflight on this diffusers build** (`gemm_w4a4_launch_impl.cuh`
  assertion), so `nunchaku` silently resolves to `4bit`. Keep `LOCAL_QUANT=4bit` until a
  matching wheel lands - setting `nunchaku` buys nothing and costs a preflight per process
- **Adapter identity comes from tensor names, not metadata.** `modelprobe` fingerprints the
  weights first and only falls back to a declared `ss_base_model_version`: trainers stamp
  stale defaults (a real Flux LoRA here declares `sd_1.5`). Fingerprints must cover BOTH
  namings per family - diffusers (`single_transformer_blocks`, `layers.N.attention`) and the
  original BFL/ComfyUI (`double_blocks`, `vector_in`) - because an unrecognised family makes
  `compatible()` answer "no judgement" (True), so every adapter is offered for every model.
- **`tests/conftest.py` redirects `LORA_DIR`** into the session temp root. `test_loras_catalog`
  writes fixtures into `settings.loras_dir` and unlinks them, so without the redirect
  `make test` deletes the developer's real (multi-GB) LoRA library. It did, once.
- `backend/app/loras.py` - LoRA catalogue over project-local `models/loras`. `sanitize()` runs at the
  **router**, not the handler: `loras` is user-supplied, persisted into job params and
  replayed verbatim on rerun, so a traversal path accepted once is replayed forever
- `backend/app/outpaint.py` - pure outpaint geometry (plan/build/composite), no torch
- `backend/app/remote_gpu_client.py` - submit+poll protocol (Cloudflare ~100s limit ⇒ token/poll), bounded retry, **secret redaction on every surfaced error**
- `remote_gpu.py` - the entire operator-controlled Remote GPU server, one file. `make_remote_gpu.py` locates
  the injected constants **by AST**, so reformatting no longer breaks `make remote-gpu`, and
  injects Python literals via `repr()` (JSON's lowercase `false` is a valid Python
  identifier - it parses, then NameErrors on the A100). `/health` reports `features`,
  `worker_alive`, `queue_depth`, `speed_loaded` and `build`; the registry gates Remote GPU
  controls on those features through `_remote_has()`, so the UI never offers what the live
  session cannot actually do
- `backend/app/enrichment.py` - async post-save worker (Florence-2 captions/auto-tags on CPU); never blocks generation
- `backend/app/variant_sets/` - Variant Sets: named axes x one template over shared sources.
  **No new job kind and no new job status**: children are ordinary jobs of existing kinds,
  built by `routers.images.build_params` and created by `routers.common.submit_group` (the
  one fan-out primitive, also behind grids and combinatorial prompts) under the set's
  `vset-<uuid>` `group_id`. Pending/blocked/validation state lives on `variantitem`; a job
  exists only once an item can run. Child request ids are scoped by that random group id,
  never by row ids alone (a deleted set's ids were once recycled and its jobs adopted), and
  the three tables are AUTOINCREMENT. Completion is a compare-and-set on the finishing job;
  `queue.on_job_terminal` is an optimisation and `reconcile_set` (startup + every read) is
  the truth. The recipe snapshot on a set is immutable; retry reuses an item's stored
  effective params. Nothing in the package may know what an axis *means*
- `backend/app/finishing.py` + `backend/app/validators/` - named finishing processors behind
  the `finish_steps` registry control (type `finishing`; `registry()` attaches the processor
  catalogue to it; runs after the unchanged `finish` preset; a failed step keeps the prior
  image and goes to `warnings`, never to `post`), and Pillow-only output checks. A Variant
  Set stage has its own `finishing` list, so `finish_steps` is in `SET_CONTROLLED`. Background removal is BiRefNet-lite ONNX (MIT, pinned + SHA-256, in NOTICE);
  BRIA RMBG weights are non-commercial and must not be used
- `backend/app/exports.py` - the single ZIP writer for gallery and Variant Set exports; the
  `embed_metadata` privacy setting governs manifests, and server paths never enter one
- `backend/app/routers/job_api.py` - **the API is a first-class client surface** (guide:
  `docs/api.md`). `POST /api/jobs` takes `{kind, params, inputs: {images, mask, last_frame},
  request_id}` with gallery asset ids, validates params **strictly** via `controls.py` (unknown
  name or out-of-range value = 400 naming it; the multipart routes keep clamping), then builds
  through the routes' own builders and the one `submit` path, so an API job is identical to a
  form job. `GET /api/jobs/kinds` publishes inputs + a JSON Schema derived from the same
  controls. Its router is included **before** `jobs.router` (`/jobs/kinds` vs
  `/jobs/{job_id}`). `GENERATOR_INPUTS` must agree with `variant_sets/operations.py` and every
  kind must appear in `docs/api.md` - both are tests. `scripts/api_example.py` is run by the
  suite through the real queue, so a breaking API change fails it. Remote kinds are gated
  on the live worker via `remote_gpu_client.remote_gpu_status()` (re-checks `/health` when
  its answer is older than 15 s): unavailable in `/kinds`, 503 on submit, a preview
  warning and a refused Variant Set create. The suite treats the worker as connected
  (conftest `_remote_gpu_answers`); request `remote_gpu_offline` to test the opposite
- `backend/app/controls.py` - registry controls as a validation contract and JSON Schema,
  shared by the job API and Variant Set recipes. A new control `type` must be handled here
  and rendered by `DynamicControls` (`tests/test_registry.py` `VALID_TYPES`)
- `backend/app/routers/tools.py` `TOOLS` - one catalogue for tools (asset kind, controls,
  params builder, handler) read by the per-tool routes and the job API. A new tool = a
  `TOOLS` entry + `JobKind` + `_LOCAL_KINDS`/`errors._LOCAL_GPU_KINDS` if local +
  `metadata._COMPOSITE_KINDS` if it keeps source pixels + frontend `LOCAL_TOOL_KINDS`.
  `matte` (BiRefNet-lite) writes `matte:cutout` or a `mask` asset
- `frontend/src/lib/generators.ts` - GEN_TO_SPEC / laneOf / HIDE_KEYS shared maps; `lib/jobEvents.ts` - pure WS reducer (previews kept OUT of the jobs map)

## Licence boundary

The project is **Apache-2.0** (`LICENSE`, attribution in `NOTICE`). That makes the
boundary below a rule, not a preference:

| Upstream | Licence | What is allowed |
|---|---|---|
| SwarmUI, techjarves repository 1258932513, open-generative-ai | MIT | Copy with attribution |
| InvokeAI, SD.Next | Apache-2.0 | Copy with attribution - add it to `NOTICE` |
| ComfyUI, krita-ai-diffusion, Fooocus | **GPL-3.0** | **Read and reimplement. Never paste.** |
| stable-diffusion-webui, PurpleDoubleD repository 1191052193 | **AGPL-3.0** | **Read and reimplement. Never paste.** |

Reimplementing an idea, an algorithm or a wire format from a GPL project is fine
and is what the two existing cases did; copying its expression is not, and would
make this repository undistributable under its own licence. Record every reuse in
`NOTICE` at the time you make it - `tests/test_licensing.py` checks the file
exists and names the boundary, but only a human can notice that a new borrowing
went unrecorded.

Temporary upstream checkouts belong only under the gitignored `explorations/`
path and must be deleted when their audit closes. Nothing that ships may depend
on a file inside it. The retained decisions and evidence live in
`docs/exploration-mining-ledger.md`.

## Conventions

- **Lazy heavy imports**: torch/diffusers/transformers/imageio/numpy/cv2/timm only inside functions. The test suite asserts none of them get imported (`tests/conftest.py` `_HEAVY` guard) - this is what lets CI run without the CUDA/imaging stack. mypy's `warn_unused_ignores` is scoped OFF for `generators/*` + `utils/io` only (their ignores target optional deps and can't satisfy both the torch-free and full envs).
- JSON-in-TEXT columns (`params_json`/`meta_json`/`tags_json`) with `@property` accessors; schemaless per-generator params.
- Routers sanitize inputs (`MAX_PROMPT` in `routers/common.py`, seeds clamped to 2³²−1, batch ≤ 8, dims snapped /16 + capped per device).
- **API views never show where the app is installed.** Job params hold absolute input paths
  and errors hold tracebacks: return jobs through `JobRead` / history through
  `PromptHistoryRead`, or pass params through `redaction.redact`, which shows paths relative
  to the app folder (`~` for the rest of the home). Stored rows keep the real paths.
- Document a route's refusals with `error_responses(...)` from `routers/common.py`; every
  `/api` router already gets 401 from `main.py`.
- Logging via `backend/app/log.py` (`log.get("tag")` - tag becomes the `[name]` prefix); level comes from `Settings.log_level` (env var beats `.env`); no `print` in `backend/`; scripts and remote_gpu.py keep print.
- Asset `generator` strings are persisted identifiers - renaming one needs a back-compat
  entry in `frontend/src/lib/generators.ts` GEN_TO_SPEC.
- **Deletes are soft.** `delete_assets` stamps `deleted_at`; only `purge_deleted` touches
  the filesystem. Every accessor (`get_asset`, `get_assets`, `search_assets`) hides trashed
  rows, so they cannot leak back into search, stats, exports or the remix pickers.
- **Adapters are a property of the selected model, not the app.** The LoRA picker carries
  `show_if` on `model_variant` listing the variants whose resolved backend can merge, because
  `resolve_backend` degrades to bitsandbytes per model - one global answer meant enabling
  nunchaku for Z-Image-Turbo also hid the picker for Chroma, Flux and SDXL.
- **A model family can override a device's sizing.** `dims_for(..., family=)` prefers a
  `"<device>_<family>"` row over the plain device row. That is how SDXL gets its ~1.05 MP
  bucket on the A100 lane instead of Qwen-Image's 1.60 MP: SDXL is area-bucketed, and past
  its trained area it renders the subject *twice* rather than larger. Its High tier
  deliberately shares Standard's area and buys steps instead.
- Controls can declare `defaults_by` - a map from another field's value to this control's
  default. That is how guidance follows `model_variant` instead of silently running a CFG
  model at Turbo's CFG 1.0.
- Post-processing is asked as one **`finish`** preset (none / faces / upscale / faces +
  upscale / custom). `resolve_finish` in `routers/common.py` expands it into the
  `post_detail`/`post_face`/`post_upscale` flags, which remain the wire format and the
  stored record of what ran. **An absent `finish` key means "custom", not "none"** - jobs
  saved before the control existed carry only the flags, and rerunning them must not
  silently drop the steps they were queued with.

## Gotchas

- `tests/conftest.py` must set its project-local temporary `OUTPUT_DIR` env **before any backend import** (module-level engine binds at import).
- **Every test module's app runs live lanes.** The `client` fixture cancels rows earlier
  modules left `queued` (they stub the queue with `no_queue`) before its lifespan resumes the
  queue, and `JobQueue.start()` replaces a worker whose event loop has closed. A test that
  enqueues without `no_queue`/a patched `enqueue` therefore really executes the handler.
- The job WebSocket's connect-time snapshot events carry `snapshot: true`, and terminal job
  events carry `group_id`; clients must not announce snapshot outcomes, and Variant Set
  children are summarised by their set, not toasted one by one.
- Wan needs 4k+1 frames (enforced in `_video_params`); the Wan-specific clamp is skipped for other engines.
- Local model swap (Turbo ↔ Quality) drops the resident pipeline - first use reloads (~1–2 min); that's why it's an explicit control, not tied to the quality tier.
- Remote GPU jobs poll `GET /result/{token}`; a UI cancel also POSTs `/cancel/{token}` so the A100
  actually stops (`pipe._interrupt`). **`_interrupt` makes the pipeline RETURN, not raise**  -
  so the worker checks the cancel flag afterwards and reports `error: "canceled"` rather than
  handing back the partially-denoised image as a success.
- `/result` does **not** free the result on read. It used to `pop()`, so a dropped tunnel
  response - the exact failure the retry loop exists for - turned every later poll into a 500.
  The client `/ack`s once it has the bytes; a TTL reclaims the rest.
- Never commit `.env`, `remote_gpu_filled.py`, `output/runtime_settings.json`.
- **`mediapipe` is capped `<0.10.30`.** It dropped `mp.solutions` there, which breaks the
  auto-detailer *and* `controlnet_aux` (which imports it at package scope, taking ControlNet
  depth/pose with it). A test guards the cap.
- **Local CPU/GPU tools must not take the whole machine.** `upscale_image` runs Real-ESRGAN
  in 512px tiles: a single pass allocated activations proportional to the *output* area
  (a 1024² input meant 64-channel maps at 4096²), which fought the resident pipeline for
  VRAM and, on the CPU fallback, stalled the desktop for seconds. `_cpu_thread_cap()` caps
  torch's CPU pool for the same reason and restores it afterwards - the pool is global and
  the diffusion pipeline does want every core.
- **ControlNet requires `LOCAL_OFFLOAD=false`.** With offload on, accelerate's hooks and the
  resident ControlNet disagree about placement and CUDA aborts the *process* - uncatchable, so
  `generate_control` refuses up front.
- A capability existing in a pipeline signature does not mean the model uses it. Wan 2.2
  TI2V-5B accepts `last_image` and discards it (no `image_encoder`), so FLF2V support is read
  from `model_index.json`, not from `inspect.signature`.
- **Keep Remote GPU model weights on the worker's local disk.** Network-mounted
  storage is normally far slower than a direct Hub fetch and makes model reloads
  unpredictable. The runner stores weights under
  `.wizards-brush-remote-gpu/models` on the remote worker's local disk; cache
  lifetime is determined by that worker, not by the local app.
- Remote GPU runtime disk is the hard ceiling on which video models are usable. LTX-2
  checkpoints are 150-200 GB and will not fit alongside Qwen (57.7 GB) and Wan
  (~31 GB); `_load_ltx` refuses with a 507 rather than starting a download that
  cannot finish, and `/health` reports `disk_free_gb` so the UI can warn first.
- **SDXL checkpoints on Remote GPU need their scheduler set.** SDXL repos ship
  `EulerDiscreteScheduler`, but the community checkpoints are tuned on DPM++ 2M SDE Karras
  (they are routinely published at 30 steps, CFG 2.5-4.5). `_tune_scheduler` in `remote_gpu.py`
  swaps it for SDXL only - Qwen/Flux/Wan ship flow-matching schedulers that are part of how
  they were trained, and swapping those breaks them.
- **The A100 holds one image model at a time.** They are 35-58 GB, so a switch is an evict
  plus a reload (plus a download on each model's first use in a session). `image_colab` jobs
  therefore carry a `model_key` and `_resident_for_lane()` reads the A100's live one from
  `/health`, so the queue groups by model instead of alternating. The picker is gated on the
  `image_alt` feature flag, not on local `.env`.
- **A batch is one job.** Assets must be announced as they are persisted
  (`_announce_asset` → a non-droppable `{"type": "asset"}` frame), or a client that refreshes
  only on the job's terminal event shows nothing until the last image of eight is done.
- Changing `remote_gpu.py` needs `make remote-gpu` **and** a Remote GPU worker
  restart. `/health` reports a `build` hash so the app can tell when the worker
  is behind.
