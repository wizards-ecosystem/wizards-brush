# Architecture

The Wizard's Brush is a single-user, local-first web application with an
optional operator-controlled remote compute lane.

```text
Browser
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
their handler disappeared; queued jobs are placed back on their lane. Database
migrations are append-only and create a backup before the first new migration.
Asset deletion is soft until the user explicitly purges Trash.

Live previews are transient binary WebSocket frames. They are not written to
SQLite or base64-encoded into JSON. Final assets are announced as soon as each
file in a batch is persisted.

## Extension points

A generator is a coordinated API contract: registry entry, persisted job kind,
handler, route, handler registration, and lane classification. Variant names and
asset generator strings are persisted identifiers and require compatibility
aliases rather than renames.

New schema changes are append-only migration modules. New model rows belong in
the lane-scoped variant catalogue. New third-party reuse must be checked against
the Apache-2.0 boundary and recorded in `NOTICE` and the exploration ledger.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for validation commands and
[SECURITY.md](../SECURITY.md) for the security policy.
