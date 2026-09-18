# Architecture

The Wizard's Brush is a single-user, local-first web application with an
optional operator-controlled remote compute lane.

```text
Browser / scripts (docs/api.md)
  | HTTP + WebSocket
  v
FastAPI application
  |-- local lane  --> global GPU lock --> Diffusers + finishing tools
  |-- remote lane --> authenticated HTTPS --> remote_gpu.py
  |-- SQLite      --> jobs, assets, presets, tags, collections
  `-- output/     --> media, thumbnails, uploads, runtime settings
```

## Components

- `frontend/` is a React/Vite SPA. Generator controls come from `GET /api/models`; React does not maintain a second model-control catalogue.
- `backend/app/main.py` creates the API, applies optional token authentication, initializes data, reconciles interrupted work, and resumes queued jobs.
- `backend/app/queue.py` owns the local and remote lanes. Every local GPU operation shares one lock.
- `backend/app/generators/` resolves model variants, loads the single resident local pipeline, validates parameters, and performs generation and finishing.
- `backend/app/db.py` owns SQLite and append-only migrations under `backend/app/migrations/`.
- `backend/app/routers/job_api.py` is the programmatic front door: `POST /api/jobs` for every generator and tool, `GET /api/jobs/kinds`, and long-poll waits (see below).
- `backend/app/variant_sets/` runs Variant Sets: durable, reference-driven fan-out over the existing operations (see below).
- `remote_gpu.py` is the complete operator-controlled worker. `make remote-gpu` injects the ignored local configuration into a generated runner.

## Data and trust boundaries

The local browser, API, database, and files are one deployment boundary. By
default the server binds to localhost and rejects non-loopback Host headers. A
non-loopback listener requires `API_TOKEN`; unsafe browser requests and every
WebSocket handshake also pass exact Origin checks before routing. See the
[network security guide](network-security.md).

Prompts, filenames, image uploads, masks, metadata imports, LoRA names, URLs,
and WebSocket messages cross input boundaries. Routers clamp and sanitize
values before durable storage so a replayed job cannot bypass the original
validation. Output and model paths must resolve inside the checkout.

The Remote GPU is a separate trust boundary. Requests use a shared secret over
public HTTPS, long work uses submit-and-poll tokens, cancellation is propagated,
and surfaced errors redact the configured secret. The operator owns provider,
network, data handling, and model-license compliance.

The public-tunnel worker authenticates and limits request bytes in pure ASGI
middleware before FastAPI parses JSON or base64 fields. Individual routes repeat
the secret check as defense in depth, request models bound fields and collection
sizes, and the in-memory queue has a finite capacity.

## Persistence and recovery

Jobs and assets are durable. A restart cancels only jobs that were running when
their handler disappeared; queued jobs are placed back on their lane. Variant Sets
are then reconciled from those job rows. Database migrations are append-only and
create a backup before the first new migration. Asset deletion is soft until the
user explicitly purges Trash.

Live previews are transient binary WebSocket frames. They are not written to
SQLite or base64-encoded into JSON. Final assets are announced as soon as each
file in a batch is persisted. The recent jobs a socket receives on connect are
marked `snapshot`, and terminal job events carry the job's `group_id`, so a
client neither re-announces old outcomes nor reports a fan-out one child at a
time.

A lane's worker is bound to the event loop of the app lifespan that started it.
A second lifespan in one process (every test module has one) replaces a worker
whose loop has closed rather than trusting it.

## The job API

The multipart generate routes exist for the browser, which holds files.
`routers/job_api.py` serves programs, which hold asset ids, and adds no second
implementation:

- `POST /api/jobs` takes `{kind, params, inputs: {images, mask, last_frame},
  request_id}`. Params are validated strictly by `backend/app/controls.py`
  against the same registry controls the UI renders: an unknown name or an
  out-of-range value is a 400 that names it, where the form routes clamp. The
  job is then built by the routes' own builders (`images.build_params`,
  `videos.build_params`) or the tool catalogue, and submitted through the one
  `submit` path, so it is indistinguishable from a form submission.
- `routers/tools.py` keeps tools in one catalogue, `TOOLS`: each entry's asset
  kind, settings as registry-style controls, params builder and handler. The
  per-tool routes and the job API both read it. The `matte` tool
  (BiRefNet-lite) writes a cutout (`matte:cutout`) or a mask asset (`mask`).
- `GET /api/jobs/kinds` publishes each kind's inputs and a JSON Schema derived
  by `controls.params_schema`, so the published contract cannot drift from the
  enforced one. `GET /api/jobs/{id}/wait` and `GET /api/variant-sets/{id}/wait`
  long-poll for at most 60 s.
- `POST /api/assets/import` brings images in (alpha kept, metadata stripped)
  and `GET /api/assets/{id}/file` serves bytes under the API's header
  authentication, which `/files` (cookie-only when a token is set) cannot.

`scripts/api_example.py` is a complete client, and the test suite runs it
through the real queue.

## Variant Sets

A Variant Set turns source material, named axes and one template into a durable
set of ordinary jobs. It adds no image-generation path and no job status.

```text
recipe (definition) --compile against the live registry--> set snapshot
set snapshot --materialize--> one item per combination per stage
item:  pending --(runnable)--> queued: child job of an existing kind
         |                        | job ends (queue.on_job_terminal)
         |                        v
         |      succeeded | invalid (failed validation) | failed | canceled
         +--(parent did not succeed)--> blocked
restart: reconcile_orphans -> resume_queued -> variant_sets.reconcile_all
```

- `backend/app/variant_sets/` holds the feature: `expansion` (axes, canonical
  keys, the cap), `templates` (single-pass `{{axis}}` substitution), `naming`
  (safe deterministic output paths), `recipe` (schema, compilation against the
  registry, materialization), `operations` (which existing kinds a set may
  drive), `store` (persistence), `service` (orchestration) and `export`.
  `routers/variant_sets.py` is the HTTP surface.
- Three tables arrive in migration `0008_variant_sets`: `variantrecipe`
  (configuration only), `variantset` (snapshot, `group_id`, counts cache,
  optional collection) and `variantitem` (key, values, effective params, job,
  attempts and history, outputs, state, validation, output name, parent). Ids
  are never reused, because deleted sets leave jobs and assets whose provenance
  names them.
- Children are created by `routers.common.submit_group`, the fan-out primitive
  X/Y grids and combinatorial prompts also use, with params built by the
  generate routes' own builders (`routers.images.build_params`). They carry the
  set's `vset-<uuid>` group id and a request id per item attempt scoped by it,
  so a submission cut short resolves to the same job, never a second one.
- `queue.on_job_terminal` tells the set a child ended. Completion is a
  compare-and-set on the finishing job; the set then validates the output,
  checks that requested finishing ran, and submits or blocks the next stage.
  The listener is an optimisation: `reconcile_set` rebuilds everything it does
  from the job rows, at startup and whenever a set is read.
- Set state rides the existing job WebSocket as `variant_set` events; there is
  no second socket.

`backend/app/finishing.py` is a registry of named processors behind the
`finish_steps` control (type `finishing`, which carries the processor catalogue)
that inline finishing runs after the unchanged `finish` preset. Any image
generator offers it; a Variant Set stage sets its own list instead. Built-ins are exact resize, alpha-preserving upscale and face restore,
and background removal (`generators/matting.py`: BiRefNet-lite as an ONNX graph,
fetched once at a pinned commit and SHA-256). `backend/app/validators/` is a
registry of deterministic Pillow-only checks returning pass/warn/fail with
details. `backend/app/exports.py` is the one ZIP writer, shared by the gallery
export and the set export.

| Route | Purpose |
| --- | --- |
| `GET /api/variant-sets/capabilities` | Operations, finishing processors, validators, cap, limits |
| `GET/POST /api/variant-recipes`, `GET/PUT/DELETE /api/variant-recipes/{id}`, `POST .../{id}/clone` | Recipe CRUD |
| `POST /api/variant-sets/preview` | Count, rendered items, collisions and warnings; creates nothing |
| `POST /api/variant-sets`, `GET /api/variant-sets`, `GET/DELETE /api/variant-sets/{id}` | Create (idempotent on `request_id`), list, detail with the recipe snapshot, delete |
| `GET /api/variant-sets/{id}/wait` | Long-poll until the set settles |
| `GET /api/variant-sets/{id}/items[/{item_id}]` | Items with live job state; `limit`/`offset` paging |
| `POST /api/variant-sets/{id}/retry`, `.../items/{item_id}/rerun`, `.../cancel` | Retry failed, rerun one, cancel remaining |
| `GET /api/variant-sets/{id}/manifest`, `POST /api/variant-sets/{id}/export` | Manifest, ZIP export |
| `POST /api/variant-sets/masks` | Store a mask as a reusable asset |

## Extension points

A generator is a coordinated API contract: registry entry, persisted job kind,
handler, route, handler registration, and lane classification. Variant names and
asset generator strings are persisted identifiers and require compatibility
aliases rather than renames.

New schema changes are append-only migration modules. New model rows belong in
the lane-scoped variant catalogue.

Variant Sets extend in four places without a schema change. A new operation is a
row in `variant_sets/operations.py` over an existing job kind. A finishing step
is one `finishing.register()` call. A validation check is one
`validators.register()` call; checks receive the asset and its source asset ids,
so a similarity check over the stored CLIP embeddings can be added later. Masks
are image assets with the `mask` generator, which recipes reference by id; the
matte tool is one producer, and a future segmentation tool only has to save
one. A new tool is one `TOOLS` entry plus a `JobKind`; the job API, its schema
and the rerun path follow from it. New third-party reuse must be checked against
the Apache-2.0 boundary and recorded in `NOTICE` and the exploration ledger.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for validation commands and
[SECURITY.md](../SECURITY.md) for the security policy.
