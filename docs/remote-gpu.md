# Remote GPU deployment

The Remote GPU lane connects a local installation of The Wizard's Brush to an
authenticated GPU worker that you operate or are explicitly authorized to use.
The worker is provider-neutral, has no provider SDK dependency, and runs as one
Python file from either a shell or an interactive notebook. It is not a shared
hosted service, and this repository does not provide GPU access.

## Deployment boundary

Use an operator-controlled GPU host or notebook runtime where you are authorized
to run the worker and expose its authenticated HTTPS endpoint. You are
responsible for the account, hardware, network exposure, model licenses, and
applicable law.

## Before you start

Provision a remote environment with:

- An NVIDIA CUDA GPU. The default remote model profile is sized for an 80 GB
  class GPU; change model slots in `.env` for smaller hardware.
- A CUDA-compatible PyTorch and torchvision installation. The generated worker
  installs a complete hash-locked application dependency closure, but
  intentionally leaves torch and numpy to the GPU runtime so it does not
  replace a working CUDA build.
- Python 3.12 on Linux x86-64, Git, and enough local ephemeral disk for the
  models you enable. The worker refuses other Python/platform combinations
  because its reviewed binary hashes would not describe their artifacts.
- A public HTTPS route to the service. The built-in Cloudflare quick tunnel is a
  convenience path for a machine you control; an operator-managed reverse proxy
  or tunnel is also suitable if it forwards HTTPS to the worker.

Do not put model caches on a slow network-mounted drive. The worker is designed
to cache on the runtime's local disk for the life of that runtime.

## Configure and start

In your local The Wizard's Brush checkout:

```bash
cp .env.example .env  # only if .env does not already exist
openssl rand -hex 32  # use this output as REMOTE_GPU_SHARED_SECRET
make remote-gpu
```

`make remote-gpu` writes `remote_gpu_filled.py`. It injects `HF_TOKEN`, the
shared secret, selected model IDs, reviewed model revision pins, the complete
hash-locked dependency set, and a build fingerprint. The tracked
`remote_gpu.py` is only a template and refuses to run directly. The generated
file is ignored and written with owner-only permissions because it contains
secrets; copy it through a private channel and do not commit or attach it to an
issue.

On the remote GPU machine:

```bash
python remote_gpu_filled.py
```

In an interactive Python notebook, upload the same generated file and run:

```python
%run remote_gpu_filled.py
```

The worker starts an authenticated FastAPI service and, by default, prints a
temporary HTTPS tunnel URL. Copy that URL into **Settings → Remote GPU** in the
local app. Enter the same shared secret; the Settings API requires it again any
time the URL host changes so a stale credential cannot be forwarded to a new
authority. The app checks `/health`; its Diagnosis page reports the connected GPU,
features, model availability, queue depth, free disk, and whether the worker is
running an older build.

## Security requirements

- Use a unique, high-entropy `REMOTE_GPU_SHARED_SECRET`. The worker refuses
  empty or common placeholder values.
- Treat the URL and secret as credentials. The URL is public at the network
  layer; the secret is the application-level protection for every request.
- Do not add a second unauthenticated proxy in front of the worker. The local
  app only accepts public HTTPS URLs and sends the secret on every request.
- Set `API_TOKEN` for the local app before exposing its own LAN
  listener beyond trusted devices.
- Rebuild and restart the worker after changing `remote_gpu.py`; the build
  fingerprint turns stale workers into a visible Diagnosis warning.
- The worker authenticates before reading a request body, caps each request and
  typed field, and caps its in-memory queue. Keep route-level authentication in
  place even when another proxy also authenticates.

## Operational notes

Only one large image model is resident on the 80 GB profile at a time. Changing
the image variant evicts the prior pipeline and can cause a reload or a first-use
download. Long video models need substantially more disk than image models; the
worker checks free space before beginning LTX downloads.

HunyuanVideo is disabled in the public starter configuration. Its Tencent
community license excludes use in the EU, UK, and South Korea and adds
hosted-service and field-of-use conditions. Read the current upstream license
and [the model-license inventory](model-licenses.md) before setting
`HUNYUAN_VIDEO_MODEL`.

The Remote GPU protocol submits a job, polls a short status endpoint, supports
idempotent retries, and acknowledges completed results. This keeps long model
loads and renders from being lost to short proxy request limits.

The complete non-Torch Python dependency closure, cloudflared, the HiDream
runner, and the project's default model repositories all use reviewed versions,
hashes, or commit revisions. Optional SageAttention is never downloaded by the
worker; enable it only after provisioning a reviewed build in the CUDA base
environment. A custom model slot is an explicit operator-controlled trust
decision and follows the update policy of the supplied repository.
