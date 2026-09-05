# Security policy

## Supported versions

Security fixes are made on `main` and released in the newest published version.
Older releases receive fixes only when maintainers can do so safely; reporters
should test against the latest release or a current commit when practical.

## Reporting a vulnerability

Do not report security vulnerabilities in public issues, discussions, pull
requests, or generated media metadata. Before this repository is made public,
maintainers must enable GitHub private vulnerability reporting. Once enabled,
report through the repository's **Security -> Report a vulnerability** flow, or
directly at `/security/advisories/new` under this repository.

Until that setting is enabled, contact a project maintainer privately through
GitHub. Include a clear description, affected version or commit, realistic
impact, and a minimal safe reproduction. Do not include secrets or working
attacks against systems you do not own or control.

Maintainers will acknowledge good-faith reports, investigate them, and
coordinate a fix before public disclosure when practical. No bug bounty or
response-time guarantee is offered.

## System and scope

The Wizard's Brush is a local-first, single-user image and video studio. The
covered product includes the FastAPI backend, browser frontend, local job queue
and SQLite library, generation and post-processing pipelines, setup and release
tooling, and the optional operator-run Remote GPU worker.

Important assets include API and Remote GPU secrets, model-service credentials,
private prompts and uploads, generated media and metadata, the local database,
the model and adapter execution boundary, and official release artifacts.

The Remote GPU worker is an internet-reachable authenticated service when its
quick tunnel is enabled. The tunnel URL is not an access boundary; the shared
secret, pre-parser authentication, request limits, and host network policy must
protect it. See [docs/remote-gpu.md](docs/remote-gpu.md) and
[docs/network-security.md](docs/network-security.md).

## Threat model and trust boundaries

Treat HTTP Host and Origin values, API and WebSocket requests, uploads,
imported metadata, release archives, model and LoRA files, remote-service
responses, and all downloaded artifacts as attacker-controlled until their
applicable boundary checks pass. LAN peers are untrusted whenever the listener
is exposed beyond loopback.

The operator, their project-local checkout, and their authenticated local
browser are trusted. Possession of the host account, write access to the
checkout, or control of the running process is equivalent to control of this
single-user application; those actors are not isolated from one another.

## Security invariants

- The application defaults to loopback. A non-loopback listener requires an API
  token, and internet or untrusted-network access requires HTTPS in front of the
  app.
- Host and browser Origin checks fail closed before API or WebSocket work.
  Origin-less command-line requests remain supported.
- Remote GPU authentication and request-size enforcement happen before body
  buffering, JSON parsing, image decoding, queueing, or GPU work.
- Uploads, archive members, decoded images, prompts, batches, paths, queues, and
  network retries remain bounded. User paths cannot escape project-owned roots.
- Reusable secrets do not appear in URLs, logs, committed files, generated
  metadata, or world-readable files. Secret-bearing POSIX files use mode `0600`.
- Only `.safetensors` LoRAs reach in-process model loaders. Executable tools,
  native extensions, default model revisions, and deserialized weight files use
  repository-reviewed immutable versions and hashes.
- Disabling generation metadata covers every external media, sidecar, static,
  and export sink while preserving the private local record needed for reruns.
- Release verification is data-only unless the operator explicitly requests a
  runtime smoke test after authenticating the archive. Build and dependency
  steps never inherit publication or attestation authority.

## Reportable findings and severity context

Report a finding when a realistic untrusted input or actor can violate an
invariant above. Relevant classes include authentication or Origin bypass,
secret disclosure, arbitrary file access, cross-user or cross-origin data
exposure, unsafe remote URL handling, supply-chain substitution, unsafe
deserialization, archive extraction or verification flaws, privilege leakage in
release automation, and remotely triggerable resource exhaustion.

Arbitrary code execution, unauthenticated access to private media or GPU work,
release-artifact compromise, and reusable credential disclosure normally merit
high severity when reachable in a supported deployment. Availability findings
must cross an unauthenticated or lower-trust boundary and have a practical,
repeatable impact; normal resource cost from an operator-requested generation is
not a vulnerability.

## Out of scope and accepted risk

- Model output quality, bias, prompt adherence, and generation of unwanted
  content are product-safety concerns rather than software vulnerabilities.
- A malicious operator, host administrator, or process with write access to the
  checkout is already inside the application's trust boundary.
- Vulnerabilities only in an unmodified upstream package or model should be
  reported upstream unless this project makes them reachable or fails a stated
  invariant.
- Direct HTTP on an operator-designated trusted LAN is an accepted deployment
  tradeoff, not a confidentiality boundary. Public or untrusted-network exposure
  without HTTPS remains reportable.
- Availability cost deliberately requested by an authenticated operator, such
  as loading a large model or rendering a long video within configured limits,
  is expected behavior.

These exclusions do not suppress a concrete authentication bypass, secret leak,
unsafe parser or loader path, or lower-trust denial of service.

## Known limitations and compensating controls

The application is not a multi-user identity system, TLS terminator, public
rate limiter, or sandbox for mutually hostile local users. Keep it on loopback
by default; use a maintained HTTPS reverse proxy or private overlay network and
a strong API token for broader access.

Custom model slots are an explicit operator trust decision and may follow an
operator-selected mutable revision. The shipped defaults, tools, and weights
remain pinned. The Remote GPU dependency lock deliberately leaves Torch, NumPy,
and the CUDA stack to the operator's compatible base runtime.

Turning off metadata prevents new disclosure and suppresses export/static
sidecars, but does not rewrite metadata already embedded in older PNG files.
Users needing retroactive removal must re-export or scrub those files.
