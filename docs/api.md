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

- [For AI agents and API tools](#for-ai-agents-and-api-tools)
- [Authentication](#authentication)
- [Conventions](#conventions)
- [Quick start](#quick-start)
- [Discovering what can run](#discovering-what-can-run)
- [Queuing a job](#queuing-a-job)
- [Following and waiting](#following-and-waiting)
- [Assets](#assets)
- [Variant Sets](#variant-sets)
- [Recipes](#recipes)
- [Saved recipes](#saved-recipes)
- [Reference sets](#reference-sets)
- [Sprite sheets and atlases](#sprite-sheets-and-atlases)
- [Finishing and validation reference](#finishing-and-validation-reference)
- [Endpoint index](#endpoint-index)

## For AI agents and API tools

A program that knows only the base URL can discover everything else from the
server itself. None of these entry points needs a token, and none contains
library data:

- `GET /llms.txt` is this guide as plain text for language models. It starts
  with the live base URL, the authentication rule and the core loop.
- `GET /openapi.json` is the OpenAPI document. It covers every route and
  schema, declares the `X-API-Token` header as its security scheme, and names
  the server's own address under `servers`, as the requesting client reached
  it. Tools that import OpenAPI, such as API clients, agent frameworks and
  custom GPT actions, can use it without editing.
- `GET /docs` and `GET /redoc` are interactive explorers over that document.
  Use **Authorize** with the token to try requests from `/docs`.

`GET /api/jobs/kinds` then gives the live list of kinds, with a JSON Schema
for each kind's settings. Like the rest of `/api`, it needs the token.

To hand the service to an AI agent, give it two facts: the base URL and the
token. Tell it to start at `<base>/llms.txt` and to send the token as
`X-API-Token` on every `/api` request. The agent needs a tool that can set
request headers, such as curl, Python or an HTTP action. A browse-only tool
cannot authenticate.

## Authentication

By default `API_TOKEN` is unset. In that state the app only answers requests
whose `Host` is loopback (`127.0.0.1`, `localhost`, `::1`), and no credential
is needed.

With `API_TOKEN` set, which is required for LAN access, send the token on every
`/api` request, as either header:

```
X-API-Token: <token>
Authorization: Bearer <token>
```

A present header is checked on its own. A wrong bearer token does not fall
through to the browser session cookie.

`/api/openai/v1` is the OpenAI image API (`GET /models`, `POST /images/generations`,
`POST /images/edits`). It uses this same token and the same Host, Origin and
Fetch-Metadata checks. A refused image call returns
`{"error": {"message", "type", "param", "code"}}` instead of `detail`.
A missing token is still `{"detail": "unauthorized"}`, because that check
runs before the route. See [Agents](agents.md).

`/files/...` URLs, such as the `url` and `thumb_url` fields on an asset, are for
browsers. They accept only the HttpOnly cookie a browser session receives, so
a program fetches bytes from `GET /api/assets/{id}/file` instead, which honours
the header. The job WebSocket (`/api/jobs/ws`) is likewise a browser channel.
Programs should use the long-poll [wait endpoints](#following-and-waiting).

See [network security](network-security.md) for LAN, TLS and CORS details.

## Conventions

- **Ids.** Inputs are gallery asset ids. An id comes from an earlier job's
  output or from `POST /api/assets/import`. Files are never passed by path.
- **Errors.** A refused request is a JSON body whose `detail` is a string that
  says what to change. Job refusals (`POST /api/jobs` and `GET /api/jobs/{id}/wait`)
  also carry `code`, and when the server knows them `field`, `valid_options`
  and up to three `suggestions`. A 422 from a malformed body (an unknown
  top-level field, for example) keeps FastAPI's list-shaped `detail`. The
  status codes mean:

  | Status | Meaning |
  | --- | --- |
  | 400 | Invalid settings or inputs |
  | 404 | Unknown id, including trashed assets |
  | 409 | Conflicting state, such as deleting an active set |
  | 422 | A malformed body, such as an unknown top-level field |
  | 503 | The device or model is not available here, or the Remote GPU is offline |
  | 401 | A missing or wrong token |

  The OpenAPI document lists the answers each route can give, with this body.
- **Paths.** Where a response mentions a file on the server, as job params and
  error tracebacks do, the path is shown relative to the app's folder, and any
  other path in your home directory as `~/...`. Where the app is installed is
  never revealed. The stored records keep the real paths.
- **Strict settings.** `POST /api/jobs`, saved recipes and Variant Set recipes
  refuse an unknown setting name or a value outside its control's range, and
  name it. The UI's multipart routes clamp instead, because a form cannot send
  such a value.
- **Idempotency.** `POST /api/jobs`, `POST /api/recipes/{key}/run` and
  `POST /api/variant-sets` accept a `request_id`: 16 to 128 characters from
  `A-Z a-z 0-9 _ -`. Sending the same id again returns the original submission
  (`"duplicate": true`, or `"created": false` for a set) instead of queuing it
  twice, so a client can retry blindly after a dropped connection. The body of
  the repeat is not compared, because the key alone identifies the submission.
- **Deletes are soft.** A deleted asset moves to the trash. It is no longer
  served or accepted as an input, and it can be restored until the trash is
  emptied.
- **Client.** Each job records who submitted it: `ui` for the browser, `mcp`
  for a tool call, or `API_TOKEN_NAME` (default `api`) when the token was sent.
  The same value is copied into output metadata and returned as `client` on
  the job and on the assets that job produced.

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
| `available`, `unavailable_reason` | False with a reason when the device probe failed, a tool's dependency is missing, or, for `remote` kinds, the Remote GPU is not connected right now. |
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
| `inpaint_remote` | remote | 1 image + mask | Masked remote inpaint on the same Edit-2511 weights; unmasked pixels stay the photograph |
| `outpaint` | local | 1 image | Extend past the frame (`direction`, `expand_pct`) |
| `control_local` | local | 1 image | ControlNet (when enabled) |
| `image_colab` | remote | none | Text to image on the Remote GPU |
| `image_edit` | remote | 1–3 images | Instruction edit; extra images are references. `input_fidelity=high` routes through `inpaint_remote` with a protect-face / named region mask |
| `refine` | remote | 1 image | Facade: one sentence over a gallery image, routed to `inpaint_remote` or `image_edit`. Each turn is an ordinary job |
| `t2v`, `long_video` | remote | none | Text to video; chained shots |
| `i2v` | remote | 1 image (+ `last_frame`) | Image to video |
| `upscale` | local | 1 image | Real-ESRGAN ×2/×4 (`scale`) |
| `face_restore` | local | 1 image | GFPGAN |
| `detail` | local | 1 image | Re-render faces/hands (`targets`, `denoise`, `prompt`) |
| `matte` | local | 1 image | Background matte: `mode` = `cutout` (transparent PNG), `mask` (subject), `inverse_mask` (surroundings) |
| `region_mask` | local | 1 image | SCHP / BiSeNet mask for hair, face, clothes, mouth or eyes (`region`, or `click_x` / `click_y`) |
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

`"dry_run": true` runs the same validation and returns the params that would
be stored, without creating a job:

```json
{"dry_run": true, "kind": "image_local", "params": {"prompt": "..."}, "would_queue": 1,
 "duplicate": false, "job_id": null, "job_ids": []}
```

`would_queue` is how many jobs a real submit would create (more than one for a
combinatorial prompt). When `request_id` already names a submission,
`duplicate` is true, `would_queue` is 0, and `job_ids` are the existing jobs.
A dry run does not write a mask file. It also does not require the Remote GPU
to be connected: a remote kind that would be 503 on submit still validates.
Submitting it for real is still 503 until the worker answers.

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

Instead of `params.prompt`, a request may send `composition`: the subject that
was typed and the style fragments to follow it, `{"subject": "a fox",
"styles": [{"id": "watercolor", "text": "soft watercolour washes"}]}`. The
server composes them the way the form does (the subject, then each text,
joined by `", "`) and keeps the parts on the job as `raw_prompt` and
`style_ids`, so reusing the job never appends a style twice. Sending both
`composition` and `params.prompt` is refused as `ambiguous_request`.

`inputs` works as follows:

- The number of `images` must fit the kind's `inputs.images` range.
- Every asset must be live (not trashed), be of the right kind, and still have
  its file.
- For `inpaint` and `inpaint_remote`, the mask must be the same size as the image.
- Gallery assets are used in place: nothing is copied.

A job created here is indistinguishable from one created in the UI. It appears
in the Queue, can be reordered, canceled, retried and rerun, and it survives a
restart while still queued.

A `remote` kind is refused with 503 while the Remote GPU is not connected,
rather than queued only to fail. The request is still validated first, so a
malformed one gets its 400.

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
    "naming": {"template": "{{colour}}/{{angle}}", "prefix": "catalogue/"}
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
  `"masks": {"region": {"asset_id": 55}}`, and an `inpaint` or `inpaint_remote` stage names it with
  `"mask": "region"`. Make masks with `POST /api/assets/import` (`role=mask`),
  the `matte` tool (`mode: mask` or `inverse_mask`), or the `region_mask` tool.
- **Stages**: a recipe has 1 to 4 stages. Stage 2 onward runs on each output
  of the previous stage, and its axes multiply. A stage may also name up to two
  `references` (asset ids) as extra inputs for `image_edit`. When a parent
  fails, its descendants are `blocked`, not lost. Retrying the parent releases
  them.
- **`naming`**: deterministic archive paths. Placeholders are the axes plus
  `{{_index}}`, `{{_key}}` and `{{_stage}}`. Names are sanitised and checked
  for collisions before anything is queued. `prefix` is put in front of every
  name exactly as written, so end it with `/` for a folder (`catalogue/`) or
  with `-` for a name prefix.

### Preset stages

A stage can run a studio preset instead of naming an operation and settings:

```json
{"stages": [{"preset": {"id": "custom/watercolor", "revision": 2},
             "prompt": "a {{pose}} fox",
             "axes": [{"name": "pose", "values": ["sitting", "running"]}]}]}
```

The set resolves the preset once, with the same resolver a single run uses,
and writes the result into the stage: the kind it runs, the preset's settings
and adapters as `params`, its composed prompt (the stage prompt fills
`{{prompt}}` as written, so axis placeholders survive), its finishing and its
validation policy. Omit `revision` for the latest; the snapshot pins the
revision and definition hash it used, so a later preset edit never changes an
existing set. Explicit stage `params`, `value_params`, a non-empty `finishing`
and a non-empty `validation` override the preset's. Preview lists each preset
stage under `presets` with its pinned revision, target and readiness. A preset
that runs on the Remote GPU and would receive images needs
`"allow_remote": true` on the stage, or the routing policy's permission to
send inputs.

### Lifecycle

```
POST /api/variant-sets/preview   {"recipe": {...}, "limit": 100}
```

`preview` returns every combination with its effective prompt, seed and output
name, plus collisions, warnings, and an `eta` (`seconds`, `confidence`,
`label`) once three comparable jobs of that kind exist. For example, it warns
about an axis that changes nothing, or a stage that needs the Remote GPU while
it is offline. It creates nothing.

```
POST /api/variant-sets
{"recipe": {...}, "name": "Spring colourways", "request_id": "...",
 "collection": {"mode": "new"}}
```

This creates the set and queues its first stage. You can pass `recipe_id` (a
saved recipe) instead of `recipe`, and `sources` replaces the recipe's
sources, so one saved recipe can run on new material. `collection` takes a
`mode` of `none`, `new` or `existing` (with an `id`). As in the editor, a set
with any stage on the Remote GPU is refused with 503 while it is offline. A
resubmitted `request_id` always returns its existing set.

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

## Sprite sheets and atlases

A finished set's final stage can be exported as a **grid sheet** (equal cells)
or a **packed atlas** (shelf-packed rectangles, optionally trimmed, across one
or more pages). Preview first; nothing is drawn or written:

```http
POST /api/variant-sets/12/sprites/preview
{"mode": "atlas", "trim": true, "padding": 2, "extrude": 1, "max_width": 1024,
 "max_height": 1024, "allow_multiple_pages": true}
```

The answer lists the pages and their sizes, every frame's page and rectangle,
and the frames that are missing. `POST /api/variant-sets/12/sprites` with the
same profile builds the PNG pages and `manifest.json`, checks them (every
rectangle inside its page, none overlapping, every frame cut back out of the
written PNG and compared with its source), zips them and publishes an
**artifact**.

| Profile field | Meaning |
| --- | --- |
| `mode` | `grid` or `atlas` |
| `order` | `set` (the set's own order) or `key` |
| `columns`, `cell_width`, `cell_height` | Grid layout; cells default to the largest frame |
| `fit` | Grid only: `require_match` (refuse other sizes), `pad` (centre unscaled) or `contain` (shrink into a fixed cell). Frames are never stretched |
| `max_width`, `max_height`, `allow_multiple_pages`, `trim` | Atlas layout |
| `padding`, `extrude` | Clear pixels around each rectangle; edge pixels repeated outward (padding must be at least twice the extrusion) |
| `background` | `transparent` or `#rrggbb` |
| `pixel_art` | Nearest-neighbour scaling, never smoothing |
| `pivot`, `durations` | Metadata written to the manifest; durations are never inferred |
| `partial` | Export what exists and list omissions, instead of refusing |

The manifest (`format: wizards-brush/atlas`, `format_version: 1`) records each
frame's stable key, index, asset id, page, `x`/`y`/`w`/`h`, original size and
trim offsets. Coordinates are top-left pixels; rectangles exclude padding and
are never rotated. A missing frame keeps its index in `missing`. With the
metadata privacy setting off, axis labels and the set name are left out.

### Artifacts

An artifact is a typed export output kept in managed storage. It has a MIME
type, size and SHA-256, and stays downloadable until `expires_at` (seven days),
not just for one response:

- `GET /api/artifacts/{id}`: the record
- `GET /api/artifacts/{id}/file`: the bytes (authenticated like every `/api` route; `X-Content-SHA256` header)
- `DELETE /api/artifacts/{id}`: remove it. The assets it was made from are untouched.

MCP: `preview_sprite_export`, `create_sprite_export`, `get_artifact`, and
`fetch_artifact` (the bytes as an embedded resource, up to 16 MB).

## Recipes

A Variant Set recipe is a saved definition of axes, templates and stages.
Editing one never changes a set already made from it, because each set keeps
its own snapshot. A saved generation recipe, which runs one ordinary job from
a short form, is a different resource: see [Saved recipes](#saved-recipes).

| Route | Purpose |
| --- | --- |
| `GET /api/variant-recipes` | List recipes |
| `POST /api/variant-recipes` | Create one: `{"name", "description", "recipe"}`. `sources` may be empty; supply them when you create the set. |
| `GET /api/variant-recipes/{id}` | Read one |
| `PUT /api/variant-recipes/{id}` | Replace one |
| `POST /api/variant-recipes/{id}/clone` | Copy one |
| `DELETE /api/variant-recipes/{id}` | Delete one |

## Preset requests and execution plans

`POST /api/jobs` takes a second request form: a preset (or an operation and a
model) plus overrides, instead of a `kind` and its `params`. The two forms
cannot be mixed in one request (`ambiguous_request`); kind-form requests are
unchanged.

```
POST /api/jobs
{
  "preset_id": "custom/storybook",
  "prompt": "a lantern festival",
  "styles": ["watercolor"],
  "settings": {"aspect": "16:9"},
  "inputs": {"images": [], "references": {"character": [41]}},
  "target": "auto",
  "dry_run": true
}
```

| Field | Meaning |
| --- | --- |
| `preset_id`, `revision` | The preset and revision (the head when omitted). |
| `operation`, `model` | Without a preset: `text_to_image`, `image_to_image`, `inpaint`, `outpaint`, `edit`, `control`, `text_to_video`, `image_to_video` or `long_video`, and a model resource id. With a preset, `model` overrides the preset's. |
| `adapters` | Ordered `[{"resource", "strength"}]`; replaces the preset's list, and `[]` means none. |
| `prompt`, `negative_prompt` | The subject the preset's template wraps; the negative replaces the preset's. |
| `styles`, `enhance`, `notes` | The preset's style ids to use; its declared enhancement text on or off; text for a `{{notes}}` placeholder. Without a preset, `style_fragments` (`[{id, text}]`) follow the prompt. |
| `settings` | Explicit overrides, named like the kind's controls. Validated strictly and never clamped. |
| `finish` | Ordered finishing steps; replaces the preset's. |
| `inputs` | `images`, `mask`, `last_frame` and `references` (asset ids per role the preset declares). |
| `target`, `allow_remote` | `local`, `remote` or `auto` (default). Auto prefers a ready local target and uses a connected remote worker only when `allow_remote` is true. Nothing here ever starts or rents hardware. |
| `seed` | Omit for a random seed. |
| `expected_plan_hash` | On submit: the `execution_plan_hash` you previewed. |

**Preparing.** With `dry_run: true` the answer is the execution plan, and
nothing is queued, downloaded, uploaded or started. A random seed is chosen
once here and returned as `seed`; send it back to keep the same request on
every preview. The plan holds:

- the preset revision and its definition hash, the logical operation, and the
  resolved target with the reason it was chosen;
- the persisted job kind it maps to, the model (resource id, revision,
  verification) and the ordered adapters with their status;
- the composed prompt (raw subject, styles, triggers, notes, enhancement
  applied, positive and negative);
- `settings` with each value's origin in `provenance` (`registry_default`,
  `dependent_default`, `preset`, `override`, `runtime`);
- `effective_params`: exactly what the job will store. They come from the same
  builders the form routes use, and every value the builder changed is listed
  in `normalized`: quality overriding steps, an aspect overriding a size,
  batch capped by the pixel budget, speed-mode recipes;
- inputs with content hashes, the seed and per-item seeds, ordered finishing
  and the validation policy;
- `readiness` (`ready`, `blockers`, `actions`), `warnings`, and
  `capability_revision`;
- `hashes`: `creative_request_hash` (what is asked: operation, resources,
  prompts, settings, inputs, seed and finishing, never the target or ids),
  `execution_plan_hash` (that request on a concrete target, kind and slot,
  with the effective params and capabilities), and `material_hash` (the
  execution hash without capabilities).

Validity and readiness are separate. A malformed or invalid request is a 400
naming the field (`settings.guidance`, `styles.1`,
`inputs.references.character`). A valid plan that cannot run yet comes back
with `ready: false` and blockers such as `resource_not_ready`,
`target_unavailable`, `access_required`, `incompatible_resource` (an adapter
for another model family, kept in the plan with `status: blocked`),
`adapters_unsupported_on_target` (the Remote GPU does not apply your adapters,
so they are refused instead of dropped) or `remote_not_allowed`. No model is
ever substituted to make a plan runnable.

**Submitting.** The same body without `dry_run` resolves the request again and
queues an ordinary job of the plan's kind, with exactly its effective params:

- A `request_id` already used for the same request returns that job
  (`duplicate: true`), even if capabilities changed since. Used for a
  different request, it is 409 `request_conflict`.
- With `expected_plan_hash`, a submission whose fresh plan would run
  differently is 409 `plan_stale`, with `state.changes` listing what changed
  when the preview is still known to this server. A capability change that
  changes nothing about what runs is not stale.
- A plan that is not ready creates no job: 503 (retryable) for an offline
  target or a missing download, 409 otherwise.
- When the job starts, its plan is checked once more. If the model slot now
  serves a different model, an adapter file moved or changed, or the Remote
  GPU worker is a different build, the job fails as `plan_stale` without
  running. The runtime that executed it (app and library versions, or the
  worker build) is recorded on the job.

`GET /api/jobs/{id}` shows `plan_id`; `GET /api/plans/{id}` returns the plan and
the request that produced it.

**Replaying.** `POST /api/jobs/{id}/replay` with `{"mode": "exact"}` queues the
stored plan again, with the same resources and seed, or answers 409
`replay_resource_unavailable` naming what is gone. It never runs a substitute.
`{"mode": "latest"}` resolves the original request against the preset's
current revision and today's resources, which is a new plan. A job made the
kind-based way has no plan and answers `legacy_unverified`; `rerun` still
queues its stored params. The same plan and seed are controlled inputs, not a
promise of identical pixels on different hardware or kernels.

## Targets and routing

`GET /api/targets` describes where work can run, read-only and without asking
any provider: a capability report for this machine and one for the Remote GPU
(identity, hardware with a `confidence` of `measured`, `advertised` or
`unknown`, the operations and kinds offered, configured model slots and
whether each is installed, adapter support, finishing processors, runtime
versions, the observation time), the routing `policy`, and a
`capability_revision`. `/api/system` carries the same revision; when it
changes, the controls a client shows may have changed.

`PUT /api/targets/policy` changes the policy:

| Setting | Default | Meaning |
| --- | --- | --- |
| `remote_enabled` | `true` | Any remote target at all. Off refuses even an explicit `target: "remote"` (`remote_disabled`). |
| `auto_may_use_remote` | `false` | Whether `auto` may route to a connected remote worker when this machine cannot run a request. A request can allow it for itself with `allow_remote`. |
| `send_inputs_to_remote` | `false` | Whether a request's images, masks and references may be sent to a remote worker without that request setting `allow_remote` (`input_transfer_not_allowed` otherwise). |

Auto prefers a ready local target, then a connected remote one it is allowed
to use, and records the reason in the plan. It never changes the model,
adapters or settings to fit a device, and nothing on the resolve or generate
path starts or rents hardware.

A plan's `readiness.state` is `ready`, `needs_resources` (a first download or
import), `needs_check` (hardware not measured yet, an adapter that could not be
matched, or a memory estimate that looks tight) or `blocked`. Only `ready`
claims the plan runs as is; `submittable` is true for all but `blocked`. A
memory estimate is advisory, as it is for the loader, which waits for memory
before it gives up; it never shrinks a request.

## Studio presets

A studio preset is a named, versioned creative setup: a model and ordered
adapters by resource id, prompt fragments, settings defaults, ordered
finishing, reference roles and a validation policy. The schema and its
vocabulary are in [studio contracts](studio-contracts.md). Curated presets
ship with the app; custom ones are saved here, by a person or an AI client,
and both share one schema. `origin` says who owns a preset, never how good it
is: a preset an AI saved is `custom`, and `client` records who saved it.

| Route | Purpose |
| --- | --- |
| `GET /api/studio/presets` | The catalogue, head revisions by name. `include_archived`, `origin`, `tag`, `q` filter it. |
| `POST /api/studio/presets` | Save a new custom preset (the definition is the body). It starts at revision 1; `id` is derived from the name when omitted, and the `curated/` namespace is reserved. |
| `GET /api/studio/presets/{id}` | One preset, its head revision or `?revision=N`. |
| `PUT /api/studio/presets/{id}` | `{"definition", "base_revision", "on_conflict"}`: save an edit as the next revision. |
| `GET /api/studio/presets/{id}/revisions` | Every revision, oldest first. |
| `POST /api/studio/presets/{id}/clone` | Copy a revision (`{"revision", "name", "id"}`, all optional) into a new custom preset. |
| `POST /api/studio/presets/{id}/archive`, `.../unarchive` | Leave or return to the catalogue. |

Revisions never change once written:

- **Every save is a new revision**, made from the revision you say you started
  from. A save whose `base_revision` is not the head is a 409
  `revision_conflict` naming the current revision; with `"on_conflict":
  "copy"` your version is saved as a new custom preset instead, so neither
  version is lost. A save that changes nothing writes no revision.
- **Curated presets are immutable.** Saving an edit to one creates a custom
  copy, and the response's `cloned_from` names the source revision. The copy
  does not inherit the curated evaluation evidence.
- **Archive, never delete.** An archived preset leaves the catalogue, but
  every revision stays readable by id and number, so jobs and Variant Set
  snapshots that name one keep resolving.

Storage checks the schema only. A preset whose model is not installed here,
or needs a larger device, still saves: readiness is a separate question,
answered when the preset is resolved.

## Reference sets

A reference set is a named, ordered collection of gallery assets grouped by
role: a character, a product, a style or any other subject. Each entry may
carry a mask painted on that asset (the same size as it) and a short note. A
set is reference material only. It is not training data, and no model is
promised to preserve an identity.

```http
POST /api/reference-sets
{"name": "Mara", "definition": {
  "notes": "red scarf, freckles",
  "roles": [{"role": "character", "label": "Mara",
             "entries": [{"asset_id": 41}, {"asset_id": 42, "mask_id": 57, "note": "profile"}]}],
  "representatives": [41]}}
```

Every save is a new immutable revision. `PUT /api/reference-sets/{id}` takes
`{definition, base_revision}` and answers `409 revision_conflict` when someone
saved after the revision you edited, so nothing is overwritten silently.
`GET /api/reference-sets/{id}?revision=N` reads any revision; archiving
(`POST .../archive`) hides a set from pickers but keeps every revision
readable.

A preset role can bind a set (`references[].reference_set`), and a preset
request can fill a role from a set:

```json
{"preset_id": "custom/character-scene", "prompt": "she reads in a cafe",
 "inputs": {"reference_sets": {"character": {"id": 3}}}}
```

Omit `revision` for the latest. The plan records the revision it used under
`plan.inputs.reference_sets`, and the job runs those exact assets: editing the
set afterwards never changes work already queued. A role takes asset ids or a
set, never both (`ambiguous_request`). A set with more or fewer references
than the role accepts is refused, never trimmed. When the role has
`notes_in_prompt` and the request sends no `notes`, the set's notes fill
`{{notes}}`.

## Resources

Models and adapters are resources: an identity that never changes, plus the
installs that hold it. A Hugging Face model is `hf:<repo>@<commit>` (or
`@unpinned` for a custom slot that follows its configured identifier); an
imported file is `local:sha256:<hash>` of its bytes. A storage location is an
install, never the identity, so two copies of one LoRA are one resource with
two installs, and installs are shown as a managed root and a relative path,
never as a path on this machine.

| Route | Purpose |
| --- | --- |
| `GET /api/resources` | Every resource: `state`, `verification`, `license`, `compat` observations and installs. Filter with `kind`, `family`, `state`. |
| `GET /api/resources/{id}` | One resource. |
| `POST /api/resources/rescan` | Re-register configured models and bring the LoRA library up to date. |
| `POST /api/resources/uploads` | `{"filename", "size", "kind", "sha256"}`: start a resumable upload. The format is checked here, before any bytes. |
| `PUT /api/resources/uploads/{id}?offset=N` | Append the body (at most `chunk_bytes`, 32 MB) at `offset`. A wrong offset is a 409 whose `state.received` says where to resume. |
| `POST /api/resources/uploads/{id}/complete` | Verify size and hash. The upload id is then a receipt. |
| `DELETE /api/resources/uploads/{id}` | Abandon an upload. Unfinished uploads are removed after a day. |
| `POST /api/resources/import` | `{"receipt", "kind", "label"}`: validate and install a completed upload. |

Importing checks four separate things, and only claims what it checked:

- **Safe handling.** Only `.safetensors` is imported. `.ckpt`, `.pt`, `.pth`,
  `.bin` and `.pkl` can run code when loaded and are refused as
  `unsafe_format`; archives are refused rather than expanded. Bytes wait in a
  quarantine directory beside their destination and are promoted with one
  atomic rename, so a refusal leaves nothing behind.
- **Structure and family.** The header is read with a bounded read and checked:
  known dtypes, shapes that match their byte ranges, ranges inside the file and
  not overlapping (`invalid_safetensors` otherwise). The tensor names give the
  family. A checkpoint imported as an adapter is `not_an_adapter`, an adapter
  imported as a model `not_a_model`.
- **Duplicates.** Content already installed is not imported again; the answer
  is `"status": "already_present"` with the existing resource.
- **What it does not claim.** An import does not prove the file will load or
  look good. A single-file checkpoint is catalogued with an error saying no
  loader runs it yet. Licence metadata from the file is recorded with
  `"reviewed": false`, and an absent licence is `unknown`.

**Preparing a resource on the Remote GPU.** `POST /api/resources/{id}/prepare`
copies an imported adapter to the connected worker, in the background (`GET`
the same path for progress, `DELETE` to cancel). Only the bytes and their
sha256 cross: the worker stores the file under its hash, a transfer that was
interrupted resumes at the offset the worker reports, the full hash is checked
before the file counts, and a file already there is not sent again. The
worker's copy then shows as an install with target `remote:<build>`. This is
preparation, not inference: until a worker advertises that it applies user
adapters, a plan that needs adapters on the remote target stays blocked
(`adapters_unsupported_on_target`). Hub models are not transferred; the
worker fetches those from their provider.

`GET /api/loras` items carry `resource_id` once the library scan has hashed the
file, and `null` until then. Each start hashes at most a few GB of new files;
`POST /api/resources/rescan` finishes the rest. On this machine,
`scripts/import_resource.py <file>` imports a local file through the same
checks.

## Provider imports

Hugging Face and Civitai links are imported in two named steps with different
side effects. Inspecting a link reads provider metadata and nothing else;
creating an import binds one exact file or repository commit and queues a
durable download task. Nothing downloads on a preview, and the general image
import never fetches a URL.

| Route | Purpose |
| --- | --- |
| `POST /api/resource-imports/inspect` | `{"link"}`: what the link points at. No row, no download. |
| `POST /api/resource-imports` | `{"link", "selection", "kind", "acknowledge_license"}`: queue one download. |
| `GET /api/resource-imports`, `GET /api/resource-imports/{id}` | Tasks with state, progress and the resource each produced. |
| `POST /api/resource-imports/{id}/cancel`, `.../retry` | Stop one; run a failed, interrupted or canceled one again. |

Accepted links: `https://huggingface.co/<org>/<repo>` (also `/tree/<rev>`,
`/blob/<rev>/<file>`, `/resolve/<rev>/<file>`), `hf:<org>/<repo>[@<rev>]`,
`https://civitai.com/models/<id>[?modelVersionId=<id>]`,
`https://civitai.com/api/download/models/<version>` and
`civitai:model/<id>[/version/<id>[/file/<id>]]`. Only https is accepted.

**Inspection** answers with the exact `revision` (a Hugging Face link without a
revision is pinned to the default branch's current commit), the `license`,
`access` (`public`, `gated`, `private`) and `candidates`. Each candidate has an
`id` to send back as `selection`, its size and published sha256, and
`supported` with a `reason` when it cannot be imported: pickle-based formats,
archives, non-model Civitai files and Diffusers components that only ship
pickle weights are refused. A Hugging Face repository with a `model_index.json`
offers itself as one `repo` candidate (a Diffusers pipeline); its top-level
`.safetensors` files are offered one by one. Civitai candidates carry their
version, base model and the family it maps to. When more than one candidate is
usable, `selection_required` is true and creating an import without a
selection answers `selection_required` with the options; nothing picks
"latest" for you.

**Creating an import** refuses before anything is written:

- `selection_required` / `invalid_option`, and `unsafe_format` or
  `unsupported_format` for a refused candidate;
- `missing_input` on `kind` for a single Hugging Face file: say `adapter` or
  `model` (Civitai types say it themselves);
- `access_required` for a gated or private repository while `HF_TOKEN` is not
  set;
- `license_unreviewed` (409) when the licence is unknown, until the request
  carries `acknowledge_license: true`. Civitai publishes permission flags
  rather than a licence, so its licence stays `unknown` with those flags
  recorded (`commercial_use`, `derivatives`, `credit_required`), and the
  acknowledgement is stored with who gave it. Brush records licences; it does
  not give a legal guarantee;
- `insufficient_disk` (507) and `too_large`.

Content already installed is not downloaded again: the task is `done` at once
with `error_code: "already_present"`.

**Tasks** run on a small I/O worker, never the GPU lane. A single file streams
into the intake quarantine, resumes with a ranged request after an
interruption when the provider supports it, must match the published size and
sha256 (`size_mismatch`, `hash_mismatch` otherwise, and the bytes are
discarded), and then goes through the same checks as a local upload: the
safetensors structure, `not_an_adapter` for a checkpoint offered as a LoRA,
deduplication and an atomic promotion that never overwrites a resource. The
imported file's identity is its content (`local:sha256:`), with the provider,
exact version, licence statement and source recorded on the resource. A
Diffusers repository downloads file by file at the inspected commit into the
managed Hugging Face cache, each weight checked against its LFS hash, and is
registered as `hf:<repo>@<commit>`; configure it in a model slot to use it.
States are `queued`, `running`, `canceling`, `canceled`, `failed`,
`interrupted` and `done`. A task that was running when the app stopped becomes
`interrupted` and waits for a retry; it is never marked done.

**Network rules.** Every request and every redirect hop must be https to a
host whose every resolved address is publicly routable: loopback, private,
link-local, carrier-grade NAT and cloud metadata addresses are refused as
`unsafe_destination`. Redirects are followed by hand, at most five. The
Hugging Face token is sent only to huggingface.co and `CIVITAI_API_TOKEN` only
to civitai.com, never to a download host a redirect names. Tokens and signed
query strings never appear in task rows, responses or logs. A task row holds
no credential and stores its source URL without a query string.

## Saved recipes

A saved recipe freezes one job kind and the settings that should stay put,
then publishes the few fields a later run may change. The Recipes page,
`POST /api/recipes` and the MCP tools share one list: `input_schema` is the
JSON Schema both the run panel and an agent read. Running a recipe does not
add a job kind. The server checks `inputs` against that schema, overlays them
on the frozen params, and calls `POST /api/jobs`. Queueing, provenance, the
client tag, `dry_run` and structured errors stay on that path.

| Route | Purpose |
| --- | --- |
| `POST /api/recipes` | Save one. `expose` names the settings or inputs a run may change: any control of the kind, plus `images`, `mask` or `last_frame` when the kind accepts that role. Optional `styles` freezes ordered style fragments (see below). |
| `GET /api/recipes?limit=` | Newest first. Each row includes `fields` and `input_schema`. `limit` is 1–200, default 100. |
| `GET /api/recipes/{key}` | One recipe by numeric id, slug, or name, including frozen `params` and `inputs`. |
| `PUT /api/recipes/{key}` | Replace one. The slug stays unless `slug` is sent. Jobs already queued are unchanged. |
| `DELETE /api/recipes/{key}` | Delete the definition. Queued jobs stay. |
| `POST /api/recipes/{key}/run` | `{"inputs", "dry_run", "request_id"}`. Unknown input keys are refused. `dry_run` returns the params that would be stored and does not queue a job. |

A name is 1–80 characters and cannot contain `/`. It is unique ignoring case,
so calling a recipe by name is unambiguous. The slug is 1–64 characters of
lowercase letters, digits and hyphens. Omitting `slug` derives one from the
name and appends `-2`, `-3`, … when that slug is taken. An explicit slug is
used as given, or refused with 409 when it is taken. A name or slug that is
only digits is refused. A key of 1–18 ASCII digits is looked up as an id; a
longer digit string is not an id and does not match a slug.

`params` are the kind's settings, filled with that kind's defaults at save
time. A value the author omitted, or sent equal to the resolved default, is
recorded as defaulted. A later change to a registry default does not retune
that setup while the field it depends on stays the same. When a run changes
that field — `model_variant` for guidance, `engine` where a control follows
it — defaulted values are computed again from the live kind. A value the
author set to something else stays as saved. A control added to the kind
after the save is not pinned; the job builder applies it.

The published `input_schema` is a snapshot of each field's limits at save
time, which is what the form and agents read. A run does not enforce that
snapshot for settings. Setting values are checked against the live kind
after the merge, so changing `engine` to Hunyuan accepts `num_frames: 9` on
a recipe whose saved schema still shows Wan's 25–121 range. Input roles
(`images`, `mask`, `last_frame`) keep the saved counts. A disagreement comes
back as the usual structured error. Save the recipe again to refresh the
published schema.

`styles` is an ordered list of `{"id", "text"}` style fragments, frozen with
their text at save time, so editing a style preset later does not change the
recipe. A run composes them onto its prompt exactly as Generate does: the
prompt, then each text, joined by `", "`. Generate and a recipe run with the
same subject and styles therefore send the same prompt, and the job keeps the
subject (`raw_prompt`) and fragments (`style_ids`) apart.

`inputs.images`, `inputs.mask` and `inputs.last_frame` are gallery asset ids.
An exposed image list whose saved default is empty is required at run time
when the kind needs at least one image. A frozen id must exist, and a mask
must match its image when both are set. Saving does not ask whether the kind
can run right now. A real run does, and answers 503 when it cannot. `dry_run`
does not queue a job and does not require the Remote GPU. A local GPU kind is
still refused when this machine cannot run one, the same as `POST /api/jobs`.

The run response names the recipe (`recipe_id`, `slug`, `name`) and, for a
real run, the job ids. The job's own params stay the kind's params. They do
not gain a recipe id, so rerunning that job still validates. `client` on the
recipe records who saved it (`ui`, `mcp`, or the API token's name) and is
left alone by a later update. The job records who ran it.

`clear_all` deletes jobs, assets and variant sets. Saved generation recipes
stay, as variant recipes and presets do.

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
| `unmasked_match` | Unmasked source pixels must equal the output (needs the source image and original hard mask) |
| `face_identity_min` | Minimum OpenCV SFace cosine vs the first source face (0–1) |

An output that fails a check is kept, and its item is marked `invalid` with the
reasons.

A spec may also say which checks are `required` and which are `optional`, by
name (`file`, `format`, `dimensions`, `alpha`, `corners`, `coverage`,
`margins`, `unmasked_match`, `face_identity`, `finishing`). A check that cannot
judge (no source image, no face found, no transparency to measure margins on)
reports a warning with `available: false`. For a required check that warning
becomes a failure, and a required check that did not run at all fails too: an
unknown is never allowed to pass a condition that had to hold. An optional
check's failure is recorded as a warning (`details.original_status: "fail"`)
and the item advances. Checks named in neither list behave as before. The
policy is stored with the set's snapshot, so later default changes do not
reinterpret an old set.

A preset's `validation` list (`{validator, required, options}`) compiles into
the same spec. The plan shows the result as `validation_spec`.

## Endpoint index

The OpenAPI document at `/openapi.json` is the authoritative list, grouped by
tag:

| Tag | Covers |
| --- | --- |
| `jobs` | Queue, wait, follow |
| `variant-sets` | Sets and variant recipes |
| `recipes` | Saved generation recipes |
| `assets` | Import, search, download, export |
| `tools` | Per-tool routes |
| `images`, `videos` | The UI's multipart routes |
| `openai` | OpenAI-compatible image generation and edits (`/api/openai/v1`) |
| `references` | Reference sets |
| `artifacts` | Export outputs (sprite sheets, atlases) |
| `resources`, `targets` | Models and adapters, imports, routing policy |
| `system`, `settings`, `library`, `collections`, `loras`, `wildcards`, `grid` | Supporting routes |

The multipart routes (`/api/generate/...`, `/api/tools/...`) remain supported.
They are what the UI posts, and they take a `payload` JSON field plus file
uploads. For programs, `POST /api/jobs` covers the same kinds with asset ids
and strict validation.
