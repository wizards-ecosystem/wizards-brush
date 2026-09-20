# Handoff — remote worker cutover (branch `runpod-first-class`)

**Status: half done.** `worker/` exists, is verified running, and is covered by lint,
mypy and the suite. `remote_gpu.py` also still exists and *everything still points at
it*. The tree is consistent in that state, so there is no rush — but until the cutover
lands there are two workers in the repo and only one of them is real.

This document is the remaining work. Delete it when the branch merges.

## Why this happened

The Remote GPU lane was built for Colab. The evidence is in the file: `CELL` markers for
pasting into notebook cells, `nest_asyncio` because a notebook already runs an event
loop, dependencies installed at runtime because a notebook cannot be an image, a
Cloudflare quick tunnel because Colab has no public route, and an 80 KB hash lock
injected into the source as a string literal because the deployment unit was a file you
copy.

Making RunPod work on top of that took four environment escape hatches and a function
that parses a provider's shell-profile file. The decision was to change the unit rather
than keep bolting on: **a package, built into a container.** Colab remains supported for
the maintainer personally, as untracked tooling — it is out of the published project.

## What already landed

| Commit | What |
|---|---|
| `9b52d04` | container image; `REMOTE_GPU_PREBUILT` / `REMOTE_GPU_ROOT` / `REMOTE_GPU_CONFIG_FILE`; tunnel no longer owns the process |
| `d0458b9` | `docs/runpod.md`, fork in `docs/remote-gpu.md`, AGENTS.md entries |
| `a9bff53` | config precedence fixed: env must beat the baked image config |
| `5d7c149` | RunPod host detected from `/etc/rp_environment`, not just the environment |
| `df8d239` | `worker/` package; Colab scaffolding deleted |

Verified on real hardware (A40 pod, 2026-09-20, **$0.62** total): the *old* worker
generated a real 1024×1024 image end to end through the app's queue and remote lane.
Verified locally: the *new* package imports, serves 14 routes, answers `/health` 200
with the shared secret and 401 without, and starts its job worker.

> **The package has never run on a GPU.** That gap is the single most important item
> below. Everything else is text and wiring.

## Order of work

### 1. Cut the app over to the package

- **`docker/remote-gpu/Dockerfile`** — `COPY worker/ /app/worker/` and
  `CMD ["python", "-m", "worker"]` instead of copying `remote_gpu.py`. Keep the layer
  order: the pip steps must stay above the source copy or every worker edit re-downloads
  889 MB of torch. Drop `REMOTE_GPU_PREBUILT` (the package has no bootstrap to skip) and
  `REMOTE_GPU_NO_TUNNEL` (there is no tunnel).
- **`backend/app/remote_gpu_client.py` → `local_build_id()`** — currently
  `sha256(ROOT / "remote_gpu.py")`. It must hash the **package** the same way
  `worker.build_id()` does: sorted `*.py`, name then bytes. **If these two disagree the
  app reports every worker as stale**, which is worse than not checking at all because
  the warning stops meaning anything.
- **`scripts/make_remote_gpu.py`** — keep `--config` (the image's non-secret config);
  delete the AST injection, `_write_private`, `_TARGETS`, `build_id`. Consider renaming
  the file to `make_remote_gpu_config.py` so the name stops promising injection.
- **`Makefile`** — drop the `remote-gpu` target; keep `remote-gpu-config` and
  `remote-gpu-image`.
- **Delete `remote_gpu.py`** last, once nothing references it.

### 2. Tests

`tests/test_make_remote_gpu.py` has 27 tests. They split three ways:

**Delete** — these test a contract that no longer exists:
`test_locates_every_declared_placeholder`, `test_injection_survives_reformatting`,
`test_missing_placeholder_is_the_only_hard_failure`, `test_booleans_render_as_python_not_json`,
`test_every_injected_value_is_a_pure_literal`, `test_render_produces_parseable_assignments`,
`test_render_escapes_values_that_would_break_the_source`,
`test_generated_remote_worker_is_atomic_and_private`,
`test_real_remote_gpu_py_still_carries_every_placeholder`, `test_the_server_outlives_the_tunnel`.

**Port to the package** — same intent, new target:
`test_remote_ingress_rejects_bad_secret_before_reading_body`,
`test_remote_ingress_rejects_oversized_authenticated_body_before_parsing`,
`test_remote_request_models_have_field_and_collection_bounds`,
`test_tunnel_only_runs_where_there_is_no_route` (now `runtime.is_managed_host`),
`test_runpod_is_detected_without_the_env_var`, `test_baked_image_config_is_the_weakest_source`,
`test_falsy_config_values_still_win_over_the_default`, `test_build_id_is_stable_and_ignores_secrets`,
`test_config_payload_covers_every_knob_remote_gpu_reads`,
`test_remote_dependencies_are_complete_hash_locked_without_cuda_shadow_packages` (retarget at
the Dockerfile, which is where `--require-hashes` now lives),
`test_image_never_bakes_the_secret_bearing_worker` (reword: the image must not carry `.env`).

**Keep unchanged** — these are about Lightning adapter selection, not deployment:
`test_base_pipeline_does_not_get_the_edit_adapter`, `test_edit_pipeline_gets_the_edit_adapter`,
`test_full_checkpoints_are_never_selected`, `test_prefers_four_steps_bf16_and_the_newest_version`,
`test_returns_none_when_nothing_matches_so_the_caller_can_warn`, `test_config_only_mode_ships_no_secrets`.

**The `_lift()` AST helper can partly die.** It existed because `remote_gpu.py` imports
torch at module scope and conftest's `_HEAVY` guard forbids that. `worker/runtime.py` and
`worker/config.py` import no torch, so their tests can `import` directly — much better
than executing lifted function bodies. **But `worker/api.py` and `worker/pipelines.py`
still import torch**, so the middleware and request-model tests cannot import them under
the guard. Either keep `_lift` for those two, or stub `sys.modules["torch"]` before
importing. Pick one and say why in the test module docstring.

Also retarget, both of which read `remote_gpu.py` by name today:

- `tests/test_release_readiness.py:97` — retired private model-slot fields.
- `tests/test_release_readiness.py:231` — asserts `cloudflared_sha256` and
  `"--require-hashes"` appear in the worker. **Both are now false**: cloudflared is gone
  entirely and the hashed install moved into the Dockerfile. Point the assertion there.
- `tests/test_licensing.py:58` — mentions `remote_gpu.py` in the NOTICE check.

### 3. Docs

- **`docs/remote-gpu.md`** — the "two ways to run the worker" fork now has one published
  path. Rewrite around the container; the notebook path leaves the published docs.
- **`docs/runpod.md`** — `make remote-gpu` no longer exists; the run command is
  `python -m worker`. Add the measured facts: the image is **19.1 GB**, the
  `runpod/pytorch:1.0.3-cu1290-torch280-ubuntu2404` image ships **Python 3.12.3**, and a
  cold pull of it took **~7 minutes** before the container started.
- **`docs/architecture.md:12,26`** — the diagram still points the remote lane at
  `remote_gpu.py`.
- **`README.md:165,223`** and **`SUPPORT.md:28`** — both reference
  `remote_gpu_filled.py`, which the published project no longer produces.
- **`AGENTS.md`** — the `remote_gpu.py` architecture entry, the `make remote-gpu` line,
  and the gotcha about rebuilding after edits.
- **`scripts/doctor.py:176`** checks for `remote_gpu_filled.py`; **`scripts/download_models.py:6`**
  and **`scripts/remote_tunnel.sh`** mention it in prose.

### 4. Naming honesty

Surfaced by the live test: an image generated **on an A40** was persisted with
`generator: colab_a100`, resolved through a slot named `a100_image_model`, while the
client streamed `"connecting to A100 …"`. None of that was true.

- Free to fix now — cosmetic: the `"connecting to A100"` progress string, the
  `"A100 image"` label in `routers/settings.py`, and showing `/health`'s real `gpu`
  value instead of a compile-time guess.
- **Needs care** — persisted API: the asset `generator` string `colab_a100` and the
  `colab` lane key. AGENTS.md requires a `GEN_TO_SPEC` back-compat entry in
  `frontend/src/lib/generators.ts`, and `model_variant` names replay on rerun. Do this
  additively (new name + alias), never as a bulk rename.
- `worker/config.py` already accepts both `image_model` and the legacy
  `a100_image_model`, so the worker side of this is done.

### 5. The maintainer's Colab tool (untracked)

Per the project's standing rule, this is personal tooling: untracked, excluded via
`.git/info/exclude`, never `.gitignore`, so the public repo does not name it.

It needs a flattener — concatenate `worker/*.py` in dependency order
(`runtime → config → pipelines → api`), strip the relative imports, and prepend the
secrets and the requirements lock the way `make remote-gpu` used to. Keeping it inside
the repo but untracked makes it likelier to get updated when `worker/`'s layout moves,
which it will.

### 6. Verify on a GPU — do not skip

The package has only ever run on CPU. Before this branch merges:

1. `make remote-gpu-config && make remote-gpu-image`
2. Provision a pod. **A100 SXM 80 GB at $1.39/hr is the right default** — A100 PCIe is
   $0.20 cheaper and was `Low` stock, which costs more than it saves. A40 at **$0.49/hr**
   (Secure; the catalog's $0.35 is the community price and A40 is Secure-only) is enough
   for a plumbing check with a small model.
3. Transfer and run, then drive a real generation through the app's API, not just
   `/health`. `scripts/api_example.py` or an `image_colab` job with
   `GET /api/jobs/{id}/wait`.
4. Confirm `/health`'s `build` equals `worker.build_id()` locally — that is the staleness
   check working.
5. **Terminate.** Do not stop: stopping releases the GPU and you may not get it back.

## Added since: provisioning (`7c327c6`, `c02c29d`, `6c571b6`)

The app can rent its own hardware — `backend/app/provisioners/`, the
`/api/remote-gpu/session` routes, and a Start GPU control. Covered by 20 backend
tests and 5 frontend, with one gap:

> **Every provisioner test mocks the HTTP transport. No pod has ever been created
> through this code path.** Request bodies are asserted against the REST v2
> schema, not against Runpod.

Closing it needs a **`RUNPOD_API_KEY` in `.env`**, which only the account owner can
produce — MCP OAuth authenticates tools in a session but yields no key the app can
hold. When one exists:

1. `POST /api/remote-gpu/session` creates a pod whose `base_url` answers `/health`.
2. The pod's env carries `REMOTE_GPU_SHARED_SECRET` — read it back with `get-pod`.
3. `DELETE /api/remote-gpu/session` terminates it and clears the stored session.
4. `SIGKILL` the app with a pod up, restart, and confirm the startup adoption
   warning names it and that it is still stoppable from the UI.

**There is no automatic teardown beyond Start/Stop and the clean-shutdown hook.**
An idle watchdog was built and verified on hardware (it terminated a pod 62s after
the last authenticated request), then removed on 2026-09-20 as overengineering:
the owner's call is that a forgotten pod is the owner's problem. Do not reintroduce
it without that conversation. The agreed mitigation is visibility — the running-cost
readout and the adoption warning — not automation.

The worker side is verified on an A100 (`91327bdfe88f`): package boots, build id
matches, host detection prints the proxy URL, empty secret refuses, 401 without
the secret, and three real Qwen-Image renders. Numbers are in `docs/runpod.md`.

## Traps already paid for

- **"Running" ≠ ready.** The pod reported `RUNNING` with `runtime: null` for ~7 minutes
  while it pulled the image. `ssh` answered `container not found` that whole time.
- **The SSH proxy (`ssh.runpod.io`) never registered** for either pod, and it requires a
  PTY, which breaks `scp`. Use the **direct TCP** endpoint from `get-pod`'s `runtime.ports`
  — it appears only once the container is up, and `scp` works over it normally.
- **Stopping a pod loses the GPU.** A restart failed with *"not enough free GPUs on the
  host machine"*. With no volume mounted, the container disk is wiped too, so a stop
  costs both the GPU and the model cache.
- **`RUNPOD_POD_ID` is not in a non-interactive environment** — `/etc/rp_environment`,
  sourced from `/root/.bashrc`. Already handled in `runtime.runpod_pod_id()`.
- **`str.replace` on source is dangerous.** During the split it matched a line *inside*
  `_runpod_pod_id` that was a prefix of the intended target, producing infinite
  recursion. Anchor on line numbers or full statements.

## Deliberately not started

**Serverless (queue endpoints).** Now cheap to reach — a handler is another entrypoint
over the same package — but it is a different wire format: `/run` caps payloads around
10 MB against a 32 MB single-image ceiling, so results must move to references rather
than base64. Worth doing only after pod timings say the cold-start economics justify it.
