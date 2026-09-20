# Handoff — external GPU integration (branch `runpod-first-class`)

The cutover is **done**. `worker/` is the only worker, the image builds and runs it,
and the app can rent, use and release hardware. This is what is left.

Delete this file when the branch merges.

## Verified on real hardware

A100-SXM4-80GB, 2026-09-20, ~$1.60 total across two pods:

- the `worker/` package boots and serves on a GPU
- `/health`'s `build` matches `local_build_id()` exactly
- the startup banner prints the right proxy URL, which means host detection read
  `/etc/rp_environment` — `RUNPOD_POD_ID` is absent from a non-interactive env
- empty secret refuses to boot; `401` without the secret, `200` with it
- **three real Qwen-Image-2512 renders** end to end through the app's queue and
  remote lane. Timings in `docs/runpod.md`

Also verified locally: the image builds from the package, runs, and reports a
build id identical to the checkout's.

## 1. The live provisioner test — needs you

**Every provisioner test mocks the HTTP transport. No pod has ever been created
through `backend/app/provisioners/`.** Request bodies are asserted against the
REST v2 schema, not against Runpod.

It needs a **`RUNPOD_API_KEY` in `.env`** — console.runpod.io/user/settings,
scoped if the permission model allows it for pods. No MCP tool mints keys, and
the account key Runpod injects into a pod at `/etc/rp_environment` is not a
credential to lift off rented hardware.

With one present:

1. `POST /api/remote-gpu/session` creates a pod whose `base_url` answers `/health`.
2. The pod's env carries `REMOTE_GPU_SHARED_SECRET` — read it back with `get-pod`.
3. `DELETE /api/remote-gpu/session` terminates it and clears the stored session.
4. `SIGKILL` the app with a pod up, restart, and confirm the startup adoption
   warning names it with elapsed time and spend, and that Stop still works.

Step 4 matters most: with no idle watchdog, that warning is the only thing
between a forgotten pod and an invoice.

## 2. Publishing an image — a decision, not a task

Today a fork has to install Docker, build a **19 GB** image, push it to a registry
they own, and name it in `.env` before the Start button can do anything. That is a
build-and-publish pipeline standing in front of a feature.

Publishing a prebuilt image per release collapses it to: get an API key, paste it,
press Start.

This is **not** the Runpod Hub listing that was declined — that is a maintained
marketplace entry with manual review. This is an ordinary public container image,
the same thing any project shipping a container does, tagged alongside the release
artifacts.

If it happens, the published config should be the conservative slot set: the image
carries model repositories and revision pins (no weights, no secrets), and
HunyuanVideo's licence in particular argues for leaving that slot empty in
anything distributed.

## 3. Smaller things

- **The image is 19.1 GB.** Most of it is the CUDA runtime and torch. It is a
  one-time pull per host, but it is also ~7 minutes of the cold start.
- **A second provisioner would prove the seam.** `manual` and `runpod` share an
  interface nothing else has exercised; adding Lambda or Vast is a module plus a
  registry row, and would confirm the abstraction is real rather than theoretical.
- **`output/benchmarks/` is still empty.** The numbers in `docs/runpod.md` were
  taken by hand; `make benchmark-a100` has never been run against a live worker.

## Decisions, so nobody re-opens them by accident

- **No idle watchdog.** One was built and verified on hardware — it terminated a
  pod 62 s after the last authenticated request — then removed as
  overengineering. Teardown is Start/Stop plus the clean-shutdown hook. A pod that
  nothing stops bills until something does; the mitigation is visibility, not
  automation.
- **No Colab path in the published repo.** The single-file worker and its injector
  are deleted. Colab remains as untracked personal tooling, excluded via
  `.git/info/exclude`, and would need a flattener that concatenates `worker/*.py`
  and rewrites the relative imports.
- **`colab_a100` keeps its name.** It is written into every remote asset ever made
  here, and an alias is a second name carried forever against a field nobody
  reads. The user-facing labels were fixed instead.

## Traps already paid for

- **"Running" ≠ ready.** A pod reported `RUNNING` with `runtime: null` for ~7
  minutes while it pulled. `ssh` answered `container not found` throughout.
- **The SSH proxy never registered** for either pod, and it requires a PTY, which
  breaks `scp`. Use the **direct TCP** endpoint from `get-pod`'s `runtime.ports`
  — it appears only once the container is up.
- **Stopping a pod loses the GPU.** A restart failed with *"not enough free GPUs
  on the host machine"*. With no volume, the container disk goes too.
- **PEP 668** on `runpod/pytorch` bases: `pip install` needs
  `--break-system-packages`. The Dockerfile sidesteps this with its own venv.
- **`quality` overrides `steps`** and sets the resolution as well. A sweep over
  `steps` alone returns byte-identical images — this nearly produced a fictional
  benchmark.
