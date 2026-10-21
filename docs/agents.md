# Agents

An agent drives Brush through [MCP](https://modelcontextprotocol.io), mounted
on the same app as the [HTTP API](api.md). Tools call that API. There is no
second generation path.

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

`dry_run: true` on `generate` or `edit` validates the request and returns the
params that would be stored, including `would_queue`. It does not create a
job, an asset, or a mask file. The same flag exists on `POST /api/jobs`, and
both paths share one validator. A dry run does not need the Remote GPU to be
connected. Submitting that remote kind for real is still 503 until the worker
answers.

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
