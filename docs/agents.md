# Agents

An agent drives Brush through [MCP](https://modelcontextprotocol.io) or the
OpenAI image API, both mounted on the same app as the [HTTP API](api.md).
Tools and image calls use that API. There is no second generation path.

The app has to be running (`make dev` or `make start`) before a client connects.

## Transports

Streamable HTTP, for clients that can send headers:

```
http://127.0.0.1:8000/api/mcp
```

Stdio, for clients that cannot. `scripts/brush-mcp.sh` (or `make mcp-stdio`)
relays each JSON-RPC line to that URL. It does not start a second server.
Point it elsewhere with `--url` or `BRUSH_URL`. The token is `BRUSH_API_TOKEN`.

## Authentication

With `API_TOKEN` unset, only a loopback `Host` is accepted and no credential
is required.

With `API_TOKEN` set, send it on every request:

```
Authorization: Bearer <token>
```

`X-API-Token` also works. The stdio bridge sends both, from `BRUSH_API_TOKEN`.

`/files/...` URLs are cookie-only once a token is set. Tool results include
`file_url` (`/api/assets/{id}/file`), which accepts the header.

The MCP SDK can keep its own host allow-list, but that list is fixed when
the session starts, so it cannot follow `API_TOKEN` or a LAN Host the bind
address does not name. The SDK allow-list is off. The app's Host check is
the boundary: a non-loopback Host is refused until `API_TOKEN` is set, and
then any Host that presents the token is accepted. See
[network security](network-security.md).

## Tools

| Tool | What it does |
| --- | --- |
| `list_kinds` | Kinds this install can run. `available_only` defaults to true. |
| `describe_kind` | One kind, including the JSON Schema `POST /api/jobs` enforces. |
| `generate` | Queue a generation. `kind` defaults to `image_local`. |
| `edit` | Edit images given as asset ids or `data:image/...;base64` URLs. |
| `get_job` | The job and any outputs already saved. |
| `wait_job` | Wait until the job settles, or up to 60 seconds. |
| `search_library` | Gallery search. |
| `list_recipes` | Saved recipes. Each entry includes `input_schema`. |
| `get_recipe` | One recipe by id, slug, or name, including frozen params. |
| `save_recipe` | Save a kind's settings. `expose` names the fields a later run may change. |
| `run_recipe` | Run one. `inputs` must match that recipe's `input_schema`. |

Call `list_kinds`, then `describe_kind`, before choosing settings. Unknown
names and out-of-range values are refused. Do not invent settings.

`generate` and `edit` wait for the job by default and report progress. After
about 55 seconds they return with `settled: false` and a `job_id`; call
`wait_job` to continue. `wait: false` returns as soon as the job is queued.

`request_id` (16–128 of letters, digits, `_` and `-`) makes a retry return the
original jobs.

### Images

Results carry resource links (`brush://assets/{id}`) and `file_url`. That is
the default: the bytes stay on this machine until the client fetches them.

`inline_images: true` also attaches one PNG preview per image, downscaled so
the long side is at most 512 px. Transparency is kept.

`brush://assets/{id}` is a JSON resource (the asset record, its metadata, and
`file_url`), not the PNG itself. Fetch the bytes from `file_url`.

`edit` accepts asset ids or data URLs. `http://` and `https://` URLs are
refused, so a tool call cannot make the server fetch a remote file. A data
URL is imported into the gallery when the edit is actually queued. `dry_run`
decodes and checks it in memory: no asset is added and captioning does not
start. Sending the same `request_id` again returns the original jobs and
does not import the images a second time.

### Checking a request

`dry_run: true` on `generate`, `edit` or `run_recipe` validates the request and
returns the params that would be stored, including `would_queue`. It does not
create a job, an asset, or a mask file. The same flag exists on `POST /api/jobs`
and `POST /api/recipes/{key}/run`, and the paths share one validator. A dry run
does not need the Remote GPU to be connected. Submitting that remote kind for
real is still 503 until the worker answers. A local GPU kind is still refused
when this machine cannot run one, because that probe runs before the dry run.

### Saved recipes

A saved recipe is a named setup: one job kind, its settings frozen, and a
short list of fields a later run may change. `list_recipes` and `get_recipe`
return `input_schema`, the JSON Schema `run_recipe`'s `inputs` must match.
The same schema drives the Recipes page. Tool definitions stay fixed; the
exact fields travel with the recipe, so saving one does not add a tool.

`save_recipe` takes `name`, `kind`, `params`, `expose`, and optional
`description`, `inputs` (frozen gallery asset ids), `slug` and
`field_descriptions`. The result is the saved recipe, including
`input_schema`. Its `client` is `mcp`.

`run_recipe` accepts only the exposed fields. An unknown key is
`unknown_setting`, and `valid_options` lists those fields, not every setting
of the kind. Omit a key to keep its saved default. A setting left at the
kind's default is recomputed when the run changes the field it follows, so
guidance left at the model's default moves with `model_variant`. Setting
values are checked against the live kind: the published schema is a snapshot
for discovery, and a sibling change can allow a value that snapshot's range
would have refused. A real run waits, like `generate`. The outcome adds
`recipe_id`, `recipe_slug` and `recipe_name`. The job itself is ordinary:
its client is `mcp`, and its params do not carry the recipe id, so a later
rerun still validates.

`brush://recipes` and `brush://recipes/{key}` return the same documents.

### Errors

A refused tool call has `isError: true`. The message text contains one JSON
object, the same object `POST /api/jobs` returns:

```json
{
  "detail": "'promt' is not a setting of image_local (valid: ...)",
  "code": "unknown_setting",
  "field": "promt",
  "valid_options": ["prompt"],
  "suggestions": ["Did you mean 'prompt'?"]
}
```

`detail` is always a string. `field`, `valid_options` and `suggestions` are
present when the server knows them. `suggestions` holds at most three short
lines. Codes include `unknown_setting`, `out_of_range`, `invalid_option`,
`invalid_type`, `kind_unavailable`, `remote_offline`, `unavailable`,
`not_found`, `conflict` and `invalid_request`.

A malformed HTTP body (an unknown top-level field) is still a 422 whose
`detail` is a list. That is request-shape validation, not a setting error.

## Who submitted the job

Each job stores a client label, copied into the metadata of the files it
produces and returned as `client` on the job and those assets.

| Caller | Label |
| --- | --- |
| The browser UI | `ui` |
| An MCP tool | `mcp` |
| An OpenAI image request | `openai`, or `API_TOKEN_NAME` when that is set |
| A request carrying the API token | `API_TOKEN_NAME`, or `api` when that is empty |

`API_TOKEN_NAME` is a provenance label, not a credential. It may contain
letters, digits, spaces, and `.` `_` `-`, up to 64 characters.

## Cursor

Cursor speaks Streamable HTTP. In MCP settings:

```json
{
  "mcpServers": {
    "brush": {
      "url": "http://127.0.0.1:8000/api/mcp",
      "headers": {"Authorization": "Bearer ${env:BRUSH_API_TOKEN}"}
    }
  }
}
```

Leave the header out when `API_TOKEN` is unset. Set `BRUSH_API_TOKEN` in the
environment Cursor launches from when a token is set.

## Claude Desktop

Claude Desktop speaks stdio. On the machine running Brush:

```json
{
  "mcpServers": {
    "brush": {
      "command": "/home/me/wizards-brush/scripts/brush-mcp.sh",
      "env": {"BRUSH_API_TOKEN": "…"}
    }
  }
}
```

Windows talking to a Brush install inside WSL uses `wsl` as the command. The
script path is the Linux path of this checkout:

```json
{
  "mcpServers": {
    "brush": {
      "command": "wsl",
      "args": ["-e", "/home/me/wizards-brush/scripts/brush-mcp.sh"],
      "env": {"BRUSH_API_TOKEN": "…"}
    }
  }
}
```

Omit `BRUSH_API_TOKEN` when the app has no token. The bridge prints protocol
errors as JSON-RPC on stdout and anything it could not deliver on stderr, so
the client's message stream stays valid JSON.

## OpenAI images

Base URL:

```
http://127.0.0.1:8000/api/openai/v1
```

There is no bare `/v1`. Host, token, Origin and Fetch-Metadata checks apply to
paths under `/api`. A second prefix would be a second copy of that boundary,
and clients that hard-code `https://api.openai.com/v1` already set `base_url`.

Authentication is the same as the rest of `/api`. The OpenAI SDK sends
`Authorization: Bearer`. `X-API-Token` also works. With `API_TOKEN` unset, a
loopback `Host` needs no credential.

A missing or wrong token is still `{"detail": "unauthorized"}`. That answer
comes from the shared middleware, before this route builds its own envelope.
Every other refusal from these routes is:

```json
{"error": {"message": "n must be between 1 and 8", "type": "invalid_request_error", "param": "n", "code": "out_of_range"}}
```

`message` is the same text `POST /api/jobs` puts in `detail`. `param` is the
field. `code` is the same stable code (`unknown_setting`, `out_of_range`,
`invalid_option`, `unsupported_parameter`, `invalid_request`, and the rest).
A failed job is `type: "server_error"`. A canceled job is 409. A wait that
exceeds `OPENAI_COMPAT_TIMEOUT` (default 300 seconds) is 504:

```json
{"error": {"message": "timed out after 300s waiting for job 12", "type": "timeout", "param": null, "code": "timeout", "job_id": 12}}
```

The job keeps running. Send the same `Idempotency-Key` to wait for it again.
Keys that already match the 16–128 `[A-Za-z0-9_-]` rule are stored as
`request_id`. Any other key is stored as the sha256 hex of its trimmed text.

### Models

`GET /models` lists one friendly id per configured image variant. `description`
is that variant's catalogue note. Raw ids are accepted and are not listed.

| Catalogue row | Friendly id | Raw id |
| --- | --- | --- |
| local / turbo | `brush-fast` | `local/turbo` |
| local / \<name\> | `brush-<name>` | `local/<name>` |
| remote / quality | `brush-remote` | `remote/quality` |
| remote / \<name\> | `brush-<name>` | `remote/<name>` |

`local/turbo` is `brush-fast` because that is the distilled default.
Remote `quality` is `brush-remote` so it does not collide with local
`brush-quality`. `colab/<name>` is accepted as an alias of `remote/<name>`
and is not listed. An unknown or unconfigured id is 400 `invalid_option` on
`model`. It does not fall back to another model.

### Calls

`POST /images/generations` queues `image_local` or `image_colab`.
`POST /images/edits` queues `inpaint` or `img2img` on the local lane, and
`inpaint_remote` or `image_edit` on the remote lane, depending on whether a
mask was sent.

| Field | What happens |
| --- | --- |
| `prompt` | Required. Becomes `params.prompt`. |
| `model` | A friendly or raw id. Omitted means `brush-fast`. |
| `n` | `batch`, from 1 to 8. A kind with no batch control accepts only 1. |
| `size` | A named ratio (`1:1`, `3:4`, `4:3`, `2:3`, `3:2`, `9:16`, `16:9`) becomes `aspect`. `WxH` becomes `aspect: "Custom"` plus `width` and `height`, each side 16–8192. `auto` or omitted keeps the kind's default. |
| `response_format` | `b64_json` (default) or `url` (absolute `/api/assets/{id}/file`). |
| `output_format` | `png` only. `jpeg` and `webp` are 400 `unsupported_parameter`. |
| `background` | `transparent` adds the `background_removal` finish step. `opaque` and `auto` leave the image opaque. |
| `quality`, `style`, `moderation`, `input_fidelity`, `output_compression` | Ignored. The response carries `X-Brush-Warning`. |
| `stream` | `true` is 400. `false` or omitted waits for the finished image. |
| `partial_images` | Greater than 0 is 400. |
| `user` | Dropped. It is an end-user label, not a generation setting. |

Edits take multipart `image`, `image[]` and `mask` files, or JSON data URLs
(`image`, `images[].image_url`, `mask`). An `http` or `https` URL is 400, so
this route cannot be pointed at another server.

The response is `{created, data, output_format, brush}`. `brush` holds
`job_ids`, `asset_ids`, each asset's `meta`, and `size` as the delivered
`WxH`. Top-level `size` is included only when it is one of `1024x1024`,
`1024x1536` or `1536x1024`. The official SDK's `ImagesResponse.size` is that
literal, and any other string makes the client discard a response that
already holds the image. Ratios and other pixel sizes stay on `brush.size`.

A kind with no aspect control ignores `size` and says so in `X-Brush-Warning`.
`image_edit` and `inpaint_remote` have no model picker: the id selects the
lane, and a variant other than that lane's default warns that the edit uses
the lane's edit model.

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/api/openai/v1",
    api_key=os.environ["BRUSH_API_TOKEN"],
)
image = client.images.generate(
    model="local/klein",
    prompt="a ceramic cup",
    size="1536x1024",
)
```

`GET /models` also publishes friendly ids such as `brush-fast`. Both forms
queue the same job. Leave `api_key` as any non-empty string when `API_TOKEN`
is unset; the SDK requires the argument, and the app ignores it until a token
is configured.
