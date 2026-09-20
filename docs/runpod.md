# Running the Remote GPU worker on RunPod

[The Remote GPU lane](remote-gpu.md) connects a local install of The Wizard's Brush to a
GPU worker you operate. This guide covers one way to get that hardware: renting it from
RunPod by the second.

Nothing here is required. The worker is provider-neutral and the notebook/VM path in
`docs/remote-gpu.md` is unchanged. This is the path for people who want the 80 GB-class
models without owning an 80 GB card.

You are renting the machine, so you are the operator: the account, the spend, the network
exposure, the model licences and applicable law are yours.

## Why a container here

On your own hardware, `remote_gpu.py` installing its dependencies at startup is merely
slow. On rented hardware it is the worst possible moment — a network call, a dependency
resolver and an ABI negotiation, each able to fail minutes into a metered session. It is
also a bet on the host's Python: the worker's lock is a set of CPython 3.12 manylinux
wheel hashes and it refuses to start on anything else.

`docker/remote-gpu/Dockerfile` moves all of that to your machine, once. The image picks
its own interpreter, so the 3.12 requirement is satisfied by construction.

## 1. Pick a GPU

The shipped model profile is sized for an 80 GB card: one image model is resident at a
time and they run 35–58 GB. Prices and stock below were read from RunPod's catalog on
**2026-09-19** — re-read them before you commit, they move.

| GPU | VRAM | $/hr | Stock | CUDA offered |
|---|---|---|---|---|
| **A100 SXM** | 80 GB | **1.39** | High | 12.8, 13.0–13.2 |
| RTX PRO 6000 (Server Ed.) | 96 GB | 1.69 | High | **13.0+ only** |
| H100 SXM | 80 GB | 2.69 | High | 12.8, 13.0, 13.2 |
| H200 SXM | 141 GB | 3.59 | High | 12.8–13.2 |
| A100 PCIe | 80 GB | 1.19 | Low | 12.8, 13.0, 13.2 |
| H100 PCIe | 80 GB | 1.99 | Low | 13.0 only |
| A40 | 48 GB | 0.35 | High | 12.8, 13.0, 13.2 |

**A100 SXM is the sensible default.** A100 PCIe is $0.20 cheaper and frequently out of
stock, which costs more than it saves.

Cards under 80 GB cannot hold Qwen-Image at bf16. A 48 GB A40 still runs Wan 2.2 TI2V-5B
and SDXL-class models, and is the cheapest way to smoke-test the image.

The RTX PRO 6000 is the interesting one — 96 GB of Blackwell for $0.30 over an H100 PCIe
— but RunPod lists **no CUDA 12.8 hosts for it at all**, so it needs a CUDA 13 image (see
below). Benchmark it against an A100 before committing; at these rates a card twice as
fast for 1.7× the price is the cheaper card.

## 2. Build and push the image

From the repo root, with Docker running:

```bash
make remote-gpu-config      # non-secret: model slots + reviewed revision pins + build id
make remote-gpu-image       # builds and tags with the build id
```

Then push it somewhere RunPod can pull from:

```bash
docker tag wizards-brush-remote-gpu:<build-id> <your-registry>/wizards-brush-remote-gpu:<build-id>
docker push <your-registry>/wizards-brush-remote-gpu:<build-id>
```

For a private registry, add credentials in RunPod's Console under Settings → Container
Registry and select them on the pod.

For a Blackwell card, override the CUDA line:

```bash
docker build --platform=linux/amd64 -f docker/remote-gpu/Dockerfile \
  --build-arg CUDA_IMAGE=nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 \
  --build-arg TORCH_VERSION=<a torch built for cu130> \
  --build-arg TORCHVISION_VERSION=<its matching torchvision> \
  -t wizards-brush-remote-gpu:<build-id>-cu130 .
```

Two rules that are not style preferences:

- **`--platform=linux/amd64`.** RunPod hosts are x86_64. An arm64 image built on Apple
  Silicon fails with `exec format error` after the push, not before it.
- **Never tag `latest`.** Hosts cache images per machine, so a mutable tag silently
  serves stale code — the exact failure `/health`'s `build` fingerprint exists to catch.
  Tag with the build id, which `make remote-gpu-image` does for you.

## 3. Decide where the weights live

`REMOTE_GPU_ROOT` decides this, and it is the single most consequential setting. Under it
go the HuggingFace cache, the HiDream checkout and everything else the worker downloads —
realistically 150–250 GB if you enable image, edit and video.

| Choice | Set it to | Rate | Survives |
|---|---|---|---|
| Container disk | leave default | $0.10/GB/mo, running only | nothing |
| Pod volume disk | `/workspace/wizards-brush` | $0.10 running, **$0.20 stopped** | stop, not terminate |
| Network volume | `/workspace/wizards-brush` | $0.07/GB/mo | everything |

The counter-intuitive part: a 250 GB container disk costs about **$0.04/hr**, so a
four-hour session pays $0.16 in storage and maybe $0.20 of GPU time re-pulling weights.
A 200 GB network volume is $14/month flat. **Terminate-and-redownload is cheaper until
you are using it roughly 25+ sessions a month.**

What a network volume actually buys is *waiting less*: switching image → edit → video in
one session is three cold downloads without it. It also pins every pod to one data
center, which narrows GPU availability — check your card is stocked there first.

Do not stop a pod expecting to resume it. Stopping releases the GPU, and RunPod makes no
promise it is there when you come back; a network volume plus terminate is the reliable
pattern.

## 4. Create the pod

- **Image:** the tag you pushed.
- **Expose HTTP Ports:** `8000`.
- **Container disk:** large enough for the models you enable, if you are not mounting a
  volume.
- **Environment:**

| Variable | Value |
|---|---|
| `REMOTE_GPU_SHARED_SECRET` | a real secret — `openssl rand -hex 32` |
| `HF_TOKEN` | a HuggingFace token, read-only scope is enough |
| `REMOTE_GPU_ROOT` | `/workspace/wizards-brush` if you attached a volume |

The image sets `REMOTE_GPU_PREBUILT=1` and `REMOTE_GPU_NO_TUNNEL=1` itself, and the
worker refuses to start on an empty or placeholder secret.

**The secret is not optional.** The pod proxy URL is public and RunPod adds no
authentication in front of it; the shared secret is the only thing between this GPU and
anyone who finds the hostname.

## 5. Connect the app

The worker prints its URL on startup:

```
https://<pod-id>-8000.proxy.runpod.net
```

Paste it into **Settings → Remote GPU**, with the same secret. The pod id is stable
across stop/start, so unlike the quick tunnel this URL is worth saving.

The app's Diagnosis page should then report the GPU, the feature list, model
availability, queue depth, free disk, and whether the worker is running an older build
than your checkout.

## Measured, 2026-09-20

One A100-SXM4-80GB (Secure, $1.59/hr — the $1.39 in the table above is the
community price), Qwen-Image-2512, end to end through the app: submit, queue,
remote lane, proxy, render, transfer, persist. Wall clock, not GPU time.

| Quality | Steps | Resolution | Warm |
|---|---|---|---|
| Draft | 20 | 736² | **8.0 s** |
| Standard | 30 | 1024² | **19.6 s** |
| High | 50 | 1296² | **41.0 s** |

Cold start on a fresh pod, in the order you wait for it:

| Phase | Time |
|---|---|
| Pod create → container running (14.7 GB image pull) | ~7 min |
| Qwen-Image download, ~58 GB | ~6–8 min |
| Load into VRAM + first render | 82 s |

So **click to first image is ~15 minutes cold**, and roughly two-thirds of it is
re-downloading what the last session already had. That is what a network volume
buys back, and the only thing it buys.

Note `quality` sets the step count *and* the resolution on this lane, and it
overrides an explicit `steps` in the job params. A sweep that varies `steps`
alone returns byte-identical images.

## Letting the app do it for you

Everything above is the manual path: you create the pod, you stop it. The app can
also do both, which is the difference between "a lane for people who own an A100"
and "a lane anyone can use".

Set it up once in `.env`:

```bash
REMOTE_GPU_PROVISIONER=runpod
RUNPOD_API_KEY=rpa_...          # console.runpod.io/user/settings; prefer a scoped key
RUNPOD_IMAGE=<your-registry>/wizards-brush-remote-gpu:<build-id>
RUNPOD_GPU_TYPE=NVIDIA A100-SXM4-80GB
```

A **Start GPU** control then appears in the workshop panel, showing the hourly
rate before you press it and the running spend while it is up. Starting writes
the pod's URL into settings for you; stopping terminates the pod and clears it.

**Nothing provisions on its own.** There is no timer, no start-on-launch, and no
"rent a GPU because a job was queued". The only thing that creates a pod is you
pressing the button.

### Stopping it is your job

Press **Stop**, or quit the app cleanly and it terminates the pod for you
(`REMOTE_GPU_STOP_ON_EXIT`, on by default).

**That is the whole of it.** There is no idle timer and no self-destruct: a pod
that nothing stops keeps billing until something does. A crash, an OOM kill or a
closed laptop runs no shutdown code, so in those cases the pod survives you.

This is a deliberate choice of visibility over automation. The control shows the
running cost the entire time a pod is up, and if the app restarts while one is
still running it says so at startup with the elapsed time and the spend so far —
because the alternative is finding out from an invoice. Watch the number.

### Keeping the model cache

By default a terminated pod takes its container disk with it, so the next start
re-downloads the weights. Set `RUNPOD_NETWORK_VOLUME_ID` and the worker's cache
moves to `/workspace`, which survives termination — at roughly $0.07/GB/month and
the cost of pinning every pod to that volume's data centre. The arithmetic in the
storage section above still applies: this is buying time, not money.

### Using a different provider

`REMOTE_GPU_PROVISIONER=manual` is the default and always available. Adding
another provider is a module in `backend/app/provisioners/` implementing
`configured`/`start`/`status`/`stop`/`adopt`, plus a row in that package's
registry and its own `.env` block. The rest of the app only ever learns a base
URL, so nothing else needs to know the provider exists.

## 6. Cost guard

Pods bill per second, for as long as they exist — running *or* stopped-with-disk. When
you are done, **terminate**; stopping keeps charging for the volume at double the running
rate and does not hold your GPU anyway.

RunPod's default spend cap is $80/hour across all resources, which is not a safety net at
this scale. Set a lower one.

## Troubleshooting

**Requests die at ~100 seconds with a 524.** The pod proxy runs through Cloudflare, which
caps connection time at 100s. The worker and client already handle this — every POST
returns a token immediately and the client polls `/result/{token}` — so if you see this,
something is calling the worker directly rather than through the app.

**"Running" but nothing answers.** Green means the container exists, not that the service
is up. First boot also loads a model. Give it a minute, then check the pod logs.

**Your code changes did nothing.** You are on a cached image tag. Rebuild with a new
build id and recreate the pod; `/health` reports `build` so the app's Diagnosis page will
tell you when the worker is behind your checkout.

**Zero GPU pods on restart.** You stopped a pod and someone else rented the card. Either
wait, or terminate and redeploy — and use a network volume so the data does not care.

**LTX-2 refuses with a 507.** Its checkpoints are 150–200 GB and `_load_ltx` checks free
space before starting a download that cannot finish. Unlike a fixed-disk notebook, here
you can simply provision a bigger disk — up to 4 TB.

## Model licences

The model slots baked into your config come from your local `.env`, and renting hardware
does not change what those licences permit. HunyuanVideo's community licence excludes the
EU, UK and South Korea and adds hosted-service conditions; BRIA RMBG weights are
non-commercial. If you share an image you built, its baked config travels with it. See
[the model-licence inventory](model-licenses.md).
