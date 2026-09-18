# HTTP API

Everything The Wizard's Brush does is available over HTTP, and the web UI is
just one client of that API. This guide covers the programmatic path:

1. Bring inputs in.
2. Queue generations and tools.
3. Wait for them.
4. Collect the results.
5. Run Variant Sets, which fan one source out across named axes.

The API is served by the app itself, at `http://127.0.0.1:8000` with the
default `HOST`/`PORT`. The live OpenAPI document is at `/openapi.json`, and an
interactive explorer is at `/docs`. Both describe request and response shapes.

A complete working client is in [`scripts/api_example.py`](../scripts/api_example.py).
The test suite runs it against the real app, so it stays correct.

## Contents

- [Authentication](#authentication)
- [Conventions](#conventions)
- [Quick start](#quick-start)
- [Discovering what can run](#discovering-what-can-run)
- [Queuing a job](#queuing-a-job)
- [Following and waiting](#following-and-waiting)
- [Assets](#assets)
- [Variant Sets](#variant-sets)
- [Recipes](#recipes)
- [Finishing and validation reference](#finishing-and-validation-reference)
- [Endpoint index](#endpoint-index)

## Authentication

By default `API_TOKEN` is unset. In that state the app only answers requests
whose `Host` is loopback (`127.0.0.1`, `localhost`, `::1`), and no credential
is needed.

With `API_TOKEN` set, which is required for LAN access, send the token on every
`/api` request:

```
X-API-Token: <token>
```

`/files/...` URLs, such as the `url` and `thumb_url` fields on an asset, are for
browsers. They accept only the HttpOnly cookie a browser session receives, so
a program fetches bytes from `GET /api/assets/{id}/file` instead, which honours
the header. The job WebSocket (`/api/jobs/ws`) is likewise a browser channel.
Programs should use the long-poll [wait endpoints](#following-and-waiting).

See [network security](network-security.md) for LAN, TLS and CORS details.

## Conventions

- **Ids.** Inputs are gallery asset ids. An id comes from an earlier job's
  output or from `POST /api/assets/import`. Files are never passed by path.
- **Errors.** A refused request is a JSON body `{"detail": "..."}` that says
  what to change. The status codes mean:

  | Status | Meaning |
  | --- | --- |
  | 400 | Invalid settings or inputs |
  | 404 | Unknown id, including trashed assets |
  | 409 | Conflicting state, such as deleting an active set |
  | 422 | A malformed body, such as an unknown top-level field |
  | 503 | The device or model is not available here |
  | 401 | A missing or wrong token |
- **Strict settings.** `POST /api/jobs` and Variant Set recipes refuse an
  unknown setting name or a value outside its control's range, and name it.
  The UI's multipart routes clamp instead, because a form cannot send such a
  value.
- **Idempotency.** `POST /api/jobs` and `POST /api/variant-sets` accept a
  `request_id`: 16 to 128 characters from `A-Z a-z 0-9 _ -`. Sending the same
  id again returns the original submission (`"duplicate": true`, or
  `"created": false` for a set) instead of queuing it twice, so a client can
  retry blindly after a dropped connection. The body of the repeat is not
  compared, because the key alone identifies the submission.
- **Deletes are soft.** A deleted asset moves to the trash. It is no longer
  served or accepted as an input, and it can be restored until the trash is
  emptied.

## Quick start

```bash
BASE=http://127.0.0.1:8000
# Add -H "X-API-Token: $API_TOKEN" to every call when a token is set.

# 1. Import an image (PNG/JPEG/WebP; transparency is kept).
curl -s -F file=@photo.png $BASE/api/assets/import
# → {"id": 41, "kind": "image", "width": 1024, "height": 1024, "generator": "import", ...}

# 2. Queue an edit of it.
curl -s -H 'Content-Type: application/json' $BASE/api/jobs -d '{
  "kind": "image_edit",
  "params": {"prompt": "Place the subject on a plain light grey studio backdrop"},
  "inputs": {"images": [41]},
  "request_id": "my-script-edit-000001"
}'
# → {"job_id": 97, "job_ids": [97], "group_id": null, "duplicate": false}

# 3. Wait for it (returns within 30 s; repeat while "settled" is false).
curl -s "$BASE/api/jobs/97/wait?timeout=30"
# → {"job": {"id": 97, "status": "done", ...}, "settled": true, "assets": [{"id": 42, ...}]}

# 4. Download the output.
curl -s -o edited.png $BASE/api/assets/42/file
```

The same round trip in Python, plus a Variant Set and a ZIP export:

```bash
python scripts/api_example.py photo.png --out api-example-output
```

## Discovering what can run

```
GET /api/jobs/kinds
```

This returns every kind `POST /api/jobs` accepts on this install, right now.
Each entry has the following fields:

| Field | Meaning |
| --- | --- |
| `kind` | The id to submit. |
| `category` | `generator` (makes something new) or `tool` (post-processes an asset). |
| `lane` | `local` (this machine's GPU/CPU) or `remote` (the Remote GPU worker). |
| `output` | `image` or `video`. |
| `available`, `unavailable_reason` | False with a reason when the device probe failed or a tool's dependency is missing. |
| `inputs` | `images: {min, max, asset_kind}`, `mask` (`none` or `required`), `last_frame`. |
| `params` | A JSON Schema for `params`, derived from the same registry the UI renders. Every property has its `default`; `additionalProperties` is false. |

The list is live, like the UI. A generator whose model is not configured does
not appear. ControlNet appears only with `ENABLE_CONTROLNET=true`. The i2v last
frame is offered only while the connected worker supports it.

The kinds on a typical install are listed below. Check `/api/jobs/kinds` for
the kinds and settings available on yours.

| Kind | Lane | Inputs | What it does |
| --- | --- | --- | --- |
| `image_local` | local | none | Text to image on the local GPU (model picked by `model_variant`) |
| `img2img` | local | 1 image | Transform an image |
| `inpaint` | local | 1 image + mask | Regenerate the masked region (white = change) |
| `outpaint` | local | 1 image | Extend past the frame (`direction`, `expand_pct`) |
| `control_local` | local | 1 image | ControlNet (when enabled) |
| `image_colab` | remote | none | Text to image on the Remote GPU |
| `image_edit` | remote | 1–3 images | Instruction edit; extra images are references |
| `t2v`, `long_video` | remote | none | Text to video; chained shots |
| `i2v` | remote | 1 image (+ `last_frame`) | Image to video |
| `upscale` | local | 1 image | Real-ESRGAN ×2/×4 (`scale`) |
| `face_restore` | local | 1 image | GFPGAN |
| `detail` | local | 1 image | Re-render faces/hands (`targets`, `denoise`, `prompt`) |
| `matte` | local | 1 image | Background matte: `mode` = `cutout` (transparent PNG), `mask` (subject), `inverse_mask` (surroundings) |
| `interpolate` | local | 1 video | Multiply the frame rate (`factor`, `method`) |
| `extend_video` | remote | 1 video | Continue a clip from its last frame |

## Queuing a job

```
POST /api/jobs
{
  "kind": "image_local",
  "params": {"prompt": "a ceramic cup on a wooden table", "aspect": "3:2", "batch": 2},
  "inputs": {"images": [], "mask": null, "last_frame": null},
  "request_id": "optional-idempotency-key"
}
```

Response:

```json
{"job_id": 97, "job_ids": [97], "group_id": null, "duplicate": false}
```

`params` works as follows:

- Settings you leave out take their defaults, exactly as the form would. For
  example, guidance follows the chosen `model_variant`.
- A setting that is hidden in the UI's Simple form is still a setting. Hiding
  one never changes what runs.
- `finish_steps` runs named processors after generation. See
  [Finishing and validation reference](#finishing-and-validation-reference).
  Example: `[{"processor": "background_removal"}, {"processor": "resize",
  "width": 1024, "height": 1024, "mode": "contain", "background": "transparent"}]`.
- With `"combinatorial": true`, a prompt with `{a|b|c}` choices fans out into
  one job per combination, up to 32. The response then lists every `job_ids`
  entry and a shared `group_id`.

`inputs` works as follows:

- The number of `images` must fit the kind's `inputs.images` range.
- Every asset must be live (not trashed), be of the right kind, and still have
  its file.
- For `inpaint`, the mask must be the same size as the image.
- Gallery assets are used in place: nothing is copied.

A job created here is indistinguishable from one created in the UI. It appears
in the Queue, can be reordered, canceled, retried and rerun, and it survives a
restart while still queued.

Two refused requests and their answers:

```
POST /api/jobs {"kind": "image_local", "params": {"promt": "x"}}
→ 400 {"detail": "'promt' is not a setting of image_local (valid: aspect, auto_negative, batch, ...)"}

POST /api/jobs {"kind": "image_edit", "params": {"prompt": "x"}}
→ 400 {"detail": "image_edit takes 1-3 input image(s); got 0"}
```

## Following and waiting

```
GET /api/jobs/{id}/wait?timeout=30
```

This is a long poll. It answers as soon as the job is `done`, `error` or
`canceled`, or when `timeout` expires, whichever comes first. `timeout` is at
most 60 seconds, so a proxy's idle limit never cuts the request. The answer
has three fields:

- `job`: the full job record, including `status`, `progress`, `message`,
  `error`, `tip` and `params`.
- `settled`: false means the timeout came first, so wait again.
- `assets`: every output the job saved. For a failed batch, this includes the
  images saved before the failure.

Other job routes:

| Route | Purpose |
| --- | --- |
| `GET /api/jobs?status=queued&limit=100` | Recent jobs, newest first |
| `GET /api/jobs/{id}` | One job |
| `GET /api/jobs/eta` | Remaining time per lane, with honest `estimating` states |
| `POST /api/jobs/{id}/cancel` | Cancel a queued or running job; `changed` says whether anything stopped |
| `POST /api/jobs/{id}/skip` | Skip the current item of a running batch |
| `POST /api/jobs/{id}/front` | Move a queued job to the front |
| `POST /api/jobs/{id}/rerun?reseed=false` | Run the same params again; with `reseed=true`, run with a new seed |

## Assets

**Import.** `POST /api/assets/import` takes a multipart upload.

| Field | Meaning |
| --- | --- |
| `file` | The image (required). |
| `role` | `source` (the default) or `mask`. A mask is stored single-channel, where white means change, and must not be empty. |
| `tags` | Comma-separated tags (optional). |
| `collection_id` | Also add the asset to this collection (optional). |

The file is re-encoded as PNG. Transparency and orientation are kept. EXIF,
location and any other embedded metadata are dropped. The response is the new
asset.

**Download.** `GET /api/assets/{id}/file` returns the original bytes. Add
`?download=true` to get it as an attachment with its file name.

The other asset routes:

| Route | Purpose |
| --- | --- |
| `GET /api/assets?q=&kind=&generator=&collection_id=&sort=newest&limit=&offset=` | Search the gallery (`semantic=true` ranks by meaning when embeddings are available) |
| `GET /api/assets/{id}` | One asset, with its generation metadata (`meta`) |
| `POST /api/assets/export` | `{"ids": [...]}` returns a ZIP with a library manifest; privacy settings apply |
| `DELETE /api/assets/{id}` | Move to trash |
| `POST /api/assets/bulk-delete` | Move many to trash |
| `POST /api/assets/restore` | Restore from trash |
| `POST /api/assets/{id}/tags` | Set tags |
| `POST /api/assets/{id}/favorite` | Set favourite |
| `POST /api/assets/{id}/rating` | Set rating |
| `GET /api/collections`, `POST /api/collections` | List or create collections |
| `POST /api/collections/{id}/add` | Add assets to a collection |

## Variant Sets

A Variant Set takes one to three approved source images. It expands named axes
into every combination, renders one instruction template per combination, and
runs each combination as an ordinary job of an existing kind, usually
`image_edit`. It then applies deterministic finishing, validates every output,
and keeps the result as one durable, exportable object.

### A recipe

```json
{
  "sources": [41],
  "content": {"colour": {"slate": "a cool slate blue", "moss": "a muted moss green"}},
  "seed": {"mode": "fixed", "value": 1234},
  "stages": [{
    "name": "Colourways",
    "operation": "image_edit",
    "axes": [
      {"name": "colour", "values": ["slate", "moss"]},
      {"name": "angle", "values": ["front", "side"]}
    ],
    "prompt": "Recolour the object in {{colour}}, seen from the {{angle}}",
    "params": {"quality": "High"},
    "value_params": {"angle": {"side": {"guidance": 3.5}}},
    "finishing": [
      {"processor": "background_removal"},
      {"processor": "resize", "width": 1024, "height": 1024, "mode": "contain",
       "background": "transparent"}
    ],
    "validation": {"format": "PNG", "width": 1024, "height": 1024,
                   "alpha": "required", "corners_transparent": true},
    "naming": {"template": "{{colour}}/{{angle}}", "prefix": "catalogue"}
  }]
}
```

The recipe fields, in order:

- **`sources`**: the asset ids the first stage edits. The operation decides how
  many: `image_edit` takes 1 to 3, and most others take 1.
- **`axes`**: named lists of values. Every combination of every axis becomes
  one variant, in a stable order. Values must be unique after normalisation;
  `Red` and `red` collide. The cap on combinations is `VARIANT_MAX_COMBINATIONS`
  (1000 by default). A recipe over the cap is refused, never truncated.
- **Templates**: `{{axis}}` renders the value, or its `content` text when there
  is one. `{{axis.value}}` renders the raw value. An unknown placeholder is an
  error, and so is a malformed one or one naming a later stage's axis.
- **`params`**: settings of the operation, validated like `POST /api/jobs`.
  The set supplies these itself, so they are refused here: `prompt`,
  `negative_prompt`, `seed`, `seed_mode`, `batch`, `combinatorial` and
  `finish_steps`. Use the stage's own `finishing` list instead of
  `finish_steps`.
- **`value_params`**: `axis → value → {setting: value}`, for what one axis
  value changes. Two axes that set the same setting for the same combination
  are refused.
- **`seed`**:
  - `fixed`: every variant shares one seed, so two variants differ only in
    what their axes change.
  - `per_variant`: each variant gets its own seed, derived from its key. It is
    stable across retries.
  - `-1`: resolved once at creation and recorded in the snapshot.
- **`masks`** and **`mask`**: the recipe declares
  `"masks": {"region": {"asset_id": 55}}`, and an `inpaint` stage names it with
  `"mask": "region"`. Make masks with `POST /api/assets/import` (`role=mask`)
  or with the `matte` tool (`mode: mask` or `inverse_mask`).
- **Stages**: a recipe has 1 to 4 stages. Stage 2 onward runs on each output
  of the previous stage, and its axes multiply. A stage may also name up to two
  `references` (asset ids) as extra inputs for `image_edit`. When a parent
  fails, its descendants are `blocked`, not lost. Retrying the parent releases
  them.
- **`naming`**: deterministic archive paths. Placeholders are the axes plus
  `{{_index}}`, `{{_key}}` and `{{_stage}}`. Names are sanitised and checked
  for collisions before anything is queued.

### Lifecycle

```
POST /api/variant-sets/preview   {"recipe": {...}, "limit": 100}
```

`preview` returns every combination with its effective prompt, seed and output
name, plus collisions and warnings (for example, an axis that changes nothing).
It creates nothing.

```
POST /api/variant-sets
{"recipe": {...}, "name": "Spring colourways", "request_id": "...",
 "collection": {"mode": "new"}}
```

This creates the set and queues its first stage. You can pass `recipe_id` (a
saved recipe) instead of `recipe`, and `sources` replaces the recipe's
sources, so one saved recipe can run on new material. `collection` takes a
`mode` of `none`, `new` or `existing` (with an `id`).

```
GET /api/variant-sets/{id}/wait?timeout=30&items=true
```

This is a long poll until the set is no longer `active`. It then reports one
of three statuses:

- `complete`: every variant succeeded and passed validation.
- `incomplete`: some variants failed, were invalid or were blocked.
- `canceled`.

`settled: false` means wait again.

Each item carries its `state`, `state_reason`, `values`, `key`, `job_id`,
`asset`, `validation` results and `output_name`. The states are `pending`,
`queued`, `succeeded`, `invalid` (the output failed validation), `failed`,
`canceled` and `blocked`.

| Route | Purpose |
| --- | --- |
| `GET /api/variant-sets?limit=&offset=` | Sets, newest first |
| `GET /api/variant-sets/{id}?items=true` | The set, its frozen `recipe` snapshot and its items |
| `GET /api/variant-sets/{id}/items?stage=&state=&limit=&offset=` | Items, filtered and paged |
| `GET /api/variant-sets/{id}/items/{item_id}` | One item with its job `params` |
| `POST /api/variant-sets/{id}/retry` | Run failed and invalid items again (`{"include_canceled": true}` adds canceled ones). Succeeded items are never touched. |
| `POST /api/variant-sets/{id}/items/{item_id}/rerun` | Run one variant again on purpose. `{"reseed": true}` gives a new seed; `{"cascade": true}` re-runs its descendants too. |
| `POST /api/variant-sets/{id}/cancel` | Stop the remaining work; finished items keep their results |
| `DELETE /api/variant-sets/{id}` | Remove the set record (cancel it first); its images stay in the library |
| `GET /api/variant-sets/{id}/manifest` | The export manifest as JSON |
| `POST /api/variant-sets/{id}/export` | `{"include_intermediate": false}` returns a ZIP of the succeeded outputs under their `output_name` paths, plus `wizards-brush-manifest.json` |
| `GET /api/variant-sets/capabilities` | Operations, finishing processors, validators and limits available here |

The manifest follows the app's privacy setting. With embedded metadata off,
prompts and settings are redacted from it, just as they are from exported
files.

## Recipes

A recipe is a saved definition. Editing a recipe never changes a set already
made from it, because each set keeps its own snapshot.

| Route | Purpose |
| --- | --- |
| `GET /api/variant-recipes` | List recipes |
| `POST /api/variant-recipes` | Create one: `{"name", "description", "recipe"}`. `sources` may be empty; supply them when you create the set. |
| `GET /api/variant-recipes/{id}` | Read one |
| `PUT /api/variant-recipes/{id}` | Replace one |
| `POST /api/variant-recipes/{id}/clone` | Copy one |
| `DELETE /api/variant-recipes/{id}` | Delete one |

## Finishing and validation reference

Finishing processors run in order after generation. They are available to any
image job as `params.finish_steps` and to a Variant Set stage as `finishing`.
`GET /api/variant-sets/capabilities` lists them with their options and says
whether each can run here.

| Processor | Options | Notes |
| --- | --- | --- |
| `background_removal` | none | BiRefNet-lite (MIT); writes real transparency. Downloads a 224 MB weight on first use. |
| `resize` | `width`, `height` (1–8192), `mode` (`contain`, `cover` or `stretch`), `background` (`transparent` or `#rrggbb`) | Exact canvas; contain pads, cover crops. |
| `upscale` | `scale` (`2` or `4`) | Real-ESRGAN, tiled, capped at 4096 px; keeps alpha. |
| `face_restore` | none | GFPGAN; keeps alpha. |

Validation is per stage, and every field is optional. The file must always
exist and decode.

| Check | Meaning |
| --- | --- |
| `format` | `PNG`, `JPEG` or `WEBP` |
| `width`, `height` | Exact pixel size |
| `alpha` | `required` (has an alpha channel), `forbidden` (fully opaque) or `any` |
| `corners_transparent` | All four corners at or below `transparent_threshold` |
| `min_transparent_fraction`, `max_transparent_fraction` | Share of transparent pixels |
| `safe_margin` | Clear pixels between visible content and every edge |

An output that fails a check is kept, and its item is marked `invalid` with the
reasons.

## Endpoint index

The OpenAPI document at `/openapi.json` is the authoritative list, grouped by
tag:

| Tag | Covers |
| --- | --- |
| `jobs` | Queue, wait, follow |
| `variant-sets` | Sets and recipes |
| `assets` | Import, search, download, export |
| `tools` | Per-tool routes |
| `images`, `videos` | The UI's multipart routes |
| `system`, `settings`, `library`, `collections`, `loras`, `wildcards`, `grid` | Supporting routes |

The multipart routes (`/api/generate/...`, `/api/tools/...`) remain supported.
They are what the UI posts, and they take a `payload` JSON field plus file
uploads. For programs, `POST /api/jobs` covers the same kinds with asset ids
and strict validation.
