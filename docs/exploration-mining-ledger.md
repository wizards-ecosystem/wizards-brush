# Exploration mining ledger

Completed 2026-08-31. This is the durable record of the temporary upstream
source-mining workspace. It records what was inspected, what entered Wizards
Brush, what was deliberately changed, and what was rejected. The source clones
and working notes are intentionally not part of the product tree.

The governing rule was clean-room adaptation across copyleft boundaries:
Apache-2.0 and MIT material could be adapted with attribution; GPL-3.0 and
AGPL-3.0 sources were read for behavior and independently reimplemented. See
`NOTICE` for the shipping attribution record.

## Scope and completion standard

The pass covered five complete upstream checkouts and five additional local
studio studies, one cross-cutting comparison, eight original workstream plans,
five first-pass source maps, eleven deep reads, and the one-off LUSTIFY conversion
script. A source was considered mined only after:

1. its architecture, persistence, scheduling, generation, model lifecycle, UI,
   and failure behavior had been compared with this project;
2. each transferable idea had an Adopt, Adapt, or Reject decision;
3. adopted work had a source location, regression test, or both in this tree;
4. licence-sensitive influence was recorded in `NOTICE`; and
5. no runtime or documentation dependency on the temporary source remained.

## Source inventory

| Source | Snapshot | Licence | Primary value | Final disposition |
|---|---:|---|---|---|
| ComfyUI | `5bdcb5f4edd70412e12fb04f357766cb4504e0fa` | GPL-3.0 | events, previews, persistence, OOM discipline | behavior studied; independently reimplemented |
| InvokeAI | `d6e74b36bb416eff49c05f864831a11e8473dd91` | Apache-2.0 | migrations, cache design, scheduling, downloads | patterns adapted with attribution |
| SwarmUI | `73a7a55fb91273ffeb283365a23ef8a84b8bb076` | MIT | parameter descriptors, cost-aware scheduling, notifications | patterns adapted with attribution |
| krita-ai-diffusion | `dda58d1c63e361207ccec085efbc34dbd32f1654` | GPL-3.0 | geometry, typed failures, remote state, provenance | behavior studied; independently reimplemented |
| SD.Next | `684940e015911efab2911667231946d91fef9f50` | Apache-2.0 | model probing, quant dispatch, applied-state guards | patterns adapted with attribution |
| AUTOMATIC1111 stable-diffusion-webui | source snapshot read 2026-08-30 | AGPL-3.0 | persisted formats, masking, grids, adapters, reliability | behavior studied; independently reimplemented |
| Fooocus | v2.5.5-era snapshot read 2026-08-30 | GPL-3.0 plus stricter inpaint material | simple UX, inpaint geometry, metadata, presets | behavior studied; independently reimplemented; restricted algorithm excluded |
| PurpleDoubleD repository 1191052193 | source snapshot read 2026-08-30 | AGPL-3.0 | watchdogs, lifecycle recovery, preflight ordering | behavior studied; independently reimplemented |
| techjarves repository 1258932513 | source snapshot read 2026-08-30 | MIT | capability negotiation, diagnosis, model lifecycle | patterns independently adapted with attribution |
| open-generative-ai | source snapshot read 2026-08-30 | MIT | large descriptor corpus and schema-driven UI failure modes | patterns independently adapted with attribution |
| LUSTIFY APEX v8 conversion helper | local one-off study | project-authored | private single-file SDXL conversion | rejected from product; obsolete and model-specific |

## Product decisions closed by the pass

| Decision | Outcome | Why |
|---|---|---|
| Simple versus advanced UI | Simple is the default; Full is one explicit switch; Advanced remains a meaningful disclosure | A beginner sees prompt, intent, model, size, seed, and batch. A power user can reach every supported control without a second app. Hidden non-defaults are surfaced and resettable. |
| Graph/workflow engine | Rejected | It would replace the product's main advantage - guided tasks - with a plugin/runtime platform and several new persistence formats. |
| Pipeline ownership | Stock Diffusers calls, with guarded call kwargs and explicit preprocessing/postprocessing | Owning the denoise loop would duplicate each model family and forfeit built-in img2img, inpaint, and ControlNet variants. We own geometry, metadata, diagnostics, and output validation instead. |
| Prompt encoding ownership | Rejected for now | CLIP, T5, Qwen, Flux, and SDXL require different embedding contracts. Long-prompt truncation is reported; unsupported A1111 emphasis is normalized or treated literally rather than falsely promised. |
| Model acquisition | Automatic exact-component first-use fetch plus read-only Diagnosis; no destructive model manager | It fixes disk preflight, partial-cache truth, auth, and visibility without adding URL ingestion, secret editing, or model deletion to the web attack surface. |
| Replay identity | Store both stable variant name and resolved model id; follow the name and warn when identity differs | Preserves configurable slots while keeping the historical producer truthful. |
| Partial-denoise steps | Keep Diffusers semantics; report `effective_steps` | Existing images rerun the same way while progress and metadata tell the truth. |
| File metadata | Independent metadata/provenance switches, native versioned JSON, A1111 interop reader, prefill-only imports | Reproducibility and disclosure are separate choices. Foreign files never become library rows implicitly. |
| Model safety behavior | Preserve repository-provided safety components; add no application-specific bypass | The runnable catalogue favors general-purpose models, while `API_TOKEN` controls network exposure and operators remain responsible for deployment and use. |
| CPU/Vulkan/NPU image lanes | Rejected | The studied lane gave up LoRAs, ControlNet, edit modes, batching, and previews for a path most users would not choose. An operator-controlled Remote GPU is the supported no-local-CUDA path. |
| Multiple resident diffusion models | Rejected | One modern DiT already consumes the practical host/GPU budget. Warm-model affinity avoids swaps without pretending a second model fits. |
| Plugin scripts and arbitrary external model paths | Rejected | Unsandboxed code and mutable paths outside the checkout conflict with the self-contained and security contracts. |
| Multi-user/fleet scheduling | Rejected | The queue is intentionally optimized for one workshop and two compute lanes, not fairness across tenants. |

## Cross-source implementation matrix

### Persistence, replay, and library

| Mined mechanism | Decision | Shipping evidence |
|---|---|---|
| Append-only SQLite migrations with pre-migration backup | Adopt | `backend/app/migrations/`, `backend/app/db.py`; migration and backup tests |
| Single-process database ownership | Adopt | advisory lock in `backend/app/db.py`; startup lock tests |
| Content/reference asset split | Adapt | `AssetContent` plus stable `Asset` references preserve old ids while deduplicating bytes |
| Hash while writing | Adopt | `backend/app/utils/hashing.py`, atomic writers in `backend/app/utils/io.py` |
| Soft delete before purge | Adopt | trash-aware accessors, stats, search, export, and recovery tests |
| Semantic parameter versioning | Adopt | `PARAM_SCHEMA_VERSION`, lazy `PARAM_MIGRATIONS`, and shared Job/History/Asset read boundary in `backend/app/models.py` |
| Persisted-key aliases | Adopt | append-only `PARAM_REMAPS` and `GEN_TO_SPEC`; collision tests |
| Record requested versus effective settings | Adopt | `TrackedParams`, `effective_steps`, applied LoRAs, effective sampler, model id, warnings, and structured post history |
| Ancestor recipe plus derivative operation history | Adopt | `derivative_meta`, generation/current dimensions, video JSON sidecars |
| Portable export | Adopt | ZIP manifest, file hashes, metadata, and sidecars; `tests/test_export.py` |
| Imported file adoption | Reject | metadata import prefills a form only; it never creates an Asset or claims ownership of foreign bytes |
| Eager destructive semantic migrations | Reject | lazy reads preserve rollback and avoid rewriting a user's history on an interpretation |

### Queue, remote lifecycle, and events

| Mined mechanism | Decision | Shipping evidence |
|---|---|---|
| Warm-model affinity with bounded deferral | Adopt | lane selection in `backend/app/queue.py`; affinity and next-pick tests |
| Model swap as a first-class visible state | Adopt | `LoadReporter`, model-load events, heartbeats, elapsed status, Generator and Queue rendering |
| ETA from observed durations | Adopt | `backend/app/eta.py`; confidence labels avoid fabricated precision |
| Binary preview frames | Adapt | typed big-endian frames with job id and tolerant decoding in `wsframe.py` |
| Backpressure that drops only disposable progress | Adopt/project-specific improvement | `ProgressHub` never drops state or asset events |
| Demand-driven previews | Adopt | subscriber check and preview cadence tests; no GPU-to-CPU preview work with no viewer |
| Submission idempotency | Adopt and extend | browser UUID, unique `Job.request_id`, fan-out recovery, Remote GPU `client_job_id` |
| Remote work survives server restart | Adopt | persisted remote token/client id, orphan reconciliation, result ack/TTL |
| Cancel from every phase | Adopt | local callbacks, remote pre-token cancellation, server interrupt, post-return cancel check |
| Bounded transient retry | Adopt | remote-only second attempt, cancel-aware delay, never after an asset is committed |
| Per-operation timeout budgets | Adopt | submit/poll/result budgets scale with work and bound silent gaps |
| Partial batch preservation | Adopt | committed assets survive later error, cancel, retry, and restart |
| Debounced queue notifications | Adopt | `backend/app/notify.py`; start, idle, and per-job webhooks cannot affect work |
| Three-stage remote transfer pipeline | Reject | current uploads are small relative to generation; the additional durable state machine did not justify its failure surface |
| Demand-pressure/fleet arbitration | Reject | no competing backend fleet or multi-user fairness problem exists |

### Models, memory, and loading

| Mined mechanism | Decision | Shipping evidence |
|---|---|---|
| One backend health/catalogue vocabulary | Adopt | `backend/app/backends/`, `/api/backends`, Diagnosis page |
| Ready/partial/absent rather than directory-exists | Adopt | `_cache_status`, incomplete-byte reporting, catalogue/UI tests |
| Exact-component prefetch before GPU serialization | Adopt | `DiffusionPipeline.download` and Nunchaku checkpoint fetch occur outside `GPU_LOCK`; in-lock loads are cache-only |
| Disk reserve before multi-GB fetch | Adopt | cache-volume preflight and actionable Diagnosis/error wording |
| Gated model preflight | Adopt | configured variant access reason and correctly scoped `HF_TOKEN` guidance |
| Host-RAM guard | Adapt and calibrate | live `MemAvailable`, quant-specific fractions, wait/retry, exact Nunchaku composed-artifact estimate |
| VAE working-memory estimator | Adopt | exact latent/runtime geometry decides tiling before decode |
| Reclaimable CUDA allocator accounting | Adopt | free VRAM includes inactive reserved blocks; fragmentation-aware cleanup |
| Per-job peak VRAM | Adopt | allocated/reserved high-water marks in metadata and tests |
| Allocator configuration before torch import | Adopt | expandable segments set without overwriting operator override |
| Real warm-up | Adopt | optional one-step forward pass, not merely model construction |
| Nunchaku subprocess preflight and honest fallback | Adopt/project-specific improvement | incompatible native kernels cannot abort the server; fallback is reported |
| Quantize the actual architecture components | Adopt | SDXL UNet/text-encoder-2 versus transformer/text-encoder mapping |
| Partial tensor streaming/model cache | Reject | Diffusers already streams shards and Accelerate owns placement; copying another placement owner would conflict with quantization/offload |
| Full model-manager download/delete UI | Reject | first-use exact fetch, Diagnosis, setup scripts, and cache status solve the user problem without destructive web actions |
| Automatic deletion of incomplete cache blobs | Reject | resumable Hugging Face state is reported, never destroyed behind the user's back |
| Checkpoint quarantine | Reject | a bad cache is surfaced and retryable; silently moving user model data is too destructive |

### Generation, geometry, and post-processing

| Mined mechanism | Decision | Shipping evidence |
|---|---|---|
| Input-authoritative img2img geometry | Adopt | “Match input”, exact aspect solver, non-destructive fit modes |
| Regional inpaint | Adopt | mask bounding box, context padding, model-area scaling, and source-space composite in `backend/app/inpaint.py` |
| Mask grow and blur after resampling | Adopt | server-side controls plus continuous coalesced brush strokes in `MaskEditor` |
| Composite original pixels back | Adopt | inpaint/outpaint preserve untouched source pixels instead of accepting a VAE round-trip |
| Navier–Stokes/multiscale outpaint priming | Adapt | source-independent priming in `backend/app/outpaint.py`; model output remains authoritative in new bands |
| Model-budgeted outpaint canvas | Adopt | requested expansion is reduced safely rather than sending arbitrary 2048² work to a local model |
| Effective step denominator | Adopt | flow/SDXL rounding matches the resolved family; progress and metadata agree |
| Per-variant refine recipes | Adopt | detailer uses the selected model's measured step/guidance recipe and avoids a swap |
| Seam-blended tiled Real-ESRGAN | Adopt | overlap ramps, output ceiling, progress, cancellation, and tile tests |
| Per-face restoration isolation | Adopt | one bad crop does not discard every successful face or the generated image |
| Finishing as one beginner choice | Adapt | `finish` presets expand into stable individual flags; Custom exposes advanced steps |
| Ordered post-processing pipeline | Adapt | detail → face → upscale is deliberate and recorded; arbitrary reordering rejected until measured |
| Flat/black output diagnostic | Adapt | non-destructive warning rather than an unvalidated fp32 rerun across every family |
| Scheduler family discipline | Adopt | SDXL default tuning; flow schedulers left as trained; stale sampler values normalized visibly |
| True CFG for Flux-family negative prompts | Adopt | sends `true_cfg_scale` only when the pipeline explicitly supports it |
| Hires/refine second diffusion pass | Reject for now | large runtime and per-family tuning cost; explicit upscale/detail tools are predictable and metadata-rich |
| Owning the denoise loop, CFG++, regional prompting | Reject | would fork each pipeline family and lose model-agnostic task variants |
| SD Upscale/re-diffused tiles | Reject | multiplies full generation cost per tile and creates a second seam/seed contract |
| Variation seed interpolation | Reject | changes seed semantics and adds an expert feature with no measured local-model benefit |
| LAB histogram matching | Reject | source-space composite and feathered masks fix the demonstrated seam without global color remapping |

### Prompts, adapters, grids, and controls

| Mined mechanism | Decision | Shipping evidence |
|---|---|---|
| Bounded brace and file-wildcard expansion | Adopt | cycle detection, caps, deterministic seed/item selection, preview count |
| A1111 prompt normalization | Adapt | supported emphasis syntax is normalized safely; literal mode preserves text; malformed input never fails a job |
| Raw prompt beside composed prompt | Adopt | style reuse never duplicates already-flattened templates |
| Style templates and automatic negatives | Adapt | separate typed sources with deterministic composition; no hidden “quality token” folklore |
| Tokenizer-limit warning | Adopt | loaded tokenizer's real limit produces a visible asset warning |
| Prompt scheduling/chunked custom encoding | Reject for now | requires family-specific embedding ownership and a durable syntax contract |
| Safetensors architecture fingerprint | Adapt | required/forbidden tensor markers plus trustworthy metadata; unknown never means incompatible |
| Adapter algorithm detection | Adopt | LoRA, DoRA, LoHa, LoKr, OFT, IA3, GLoRA, and full-diffusion classification |
| Adapter identity by complete relative path | Adopt | same filename in different folders cannot collide in PEFT state |
| Adapter file-change identity | Adopt | mtime/size invalidates loaded state and catalogue caches |
| Bounded catalogue caches | Adopt | LRU behavior avoids re-probing a library just over a fixed threshold |
| Variant and family compatibility | Adapt | incompatible adapters stay visible but are not applied; unknown stays usable; sidecars can pin variants |
| Separate SDXL denoiser/text-encoder weights | Adopt | advanced text weight is hidden behind a collapsed SDXL-only control and participates in cache identity |
| Adapter coverage/training-tag analytics | Reject | Diffusers does not expose reliable layer-coverage results; tag histograms add UI weight without changing safe application |
| One grid axis vocabulary | Adopt | registry marks sweepable controls; server rejects undeclared values before any cell runs |
| Expensive axis outside | Adopt | model and adapter changes are grouped to minimize reloads without changing visual X/Y positions |
| Materialized seeds and fixed base seed | Adopt | labels are truthful and non-seed studies are not confounded by random noise |
| Numeric range/count syntax | Adopt | `start-end (+step)` and `start-end [count]`, server bounded, browser previewed |
| Interactive grid rather than raster contact sheet | Adapt | every cell remains an Asset with zoom, metadata, review, and rerun; no lower-fidelity duplicate file |
| Simple/Full tiers and seized controls | Adopt | registry `tier`, `constrained_by`, dependent defaults, hidden-override disclosure |
| Narrowed dependent descriptors | Adopt | `overrides_by` changes legal label/range/options; Wan frame counts versus LTX/Hunyuan are truthful |
| Stale state coercion against live schema | Adopt | options, types, bounds, styles, dependencies, and foreign keys are normalized before render/submit |
| Generic residual/unknown-field renderer | Reject | undeclared inputs must fail tests or be reported unused, not become accidental UI |

### Files, settings, diagnosis, and security

| Mined mechanism | Decision | Shipping evidence |
|---|---|---|
| Independent reproducibility and AI-provenance switches | Adopt | runtime Settings controls plus `.env` defaults |
| Native lossless metadata plus A1111 parameters | Adopt | versioned JSON, quoted values, LoRA merge/read, EXIF fallback |
| Atomic media and sidecar writes | Adopt | temp file on destination filesystem then replace; failures cannot leave valid-looking half files |
| Per-key config fallback | Adopt | one malformed typed env value reports and defaults without discarding valid settings |
| Corrupt runtime-settings quarantine | Adopt | byte-preserving rename, warning in Settings, writes blocked if preservation fails |
| Complete configuration documentation test | Adopt | every public env key must appear in `.env.example` |
| Diagnosis instead of one-time wizard | Adopt | rerunnable GPU/profile/model/cache/disk/Remote GPU/credential page |
| Live backend capability gating | Adopt | controls follow the Remote GPU worker's reported build/features, not hopeful local config |
| Build identity across remote boundary | Adopt | Remote GPU build hash and stale warning |
| SSRF guard on outbound Remote GPU URL | Adopt | HTTPS scheme, credentials/query rejection, and literal private/loopback rejection; secret never sent to invalid target |
| Error classification with severity and retry meaning | Adapt | fatal/transient/warning/notice drives retry, UI, and GPU cleanup |
| Context-scoped authorization guidance | Adopt | local Hugging Face failures point to `.env`; Remote GPU 403 points to service credentials |
| Preserve model safety components; disable only the invisible watermarker | Adopt | repository-provided safety behavior remains active while optional watermark behavior stays deterministic |
| Browser-editable HF token | Reject | secrets remain out of API responses and runtime settings; project-root `.env` is explicit and auditable |
| Broad direct-URL/Civitai downloader | Reject | would require redirect-by-redirect SSRF validation, integrity, credentials, and destructive lifecycle management |

## Source-by-source closure

### ComfyUI

Adopted or adapted: binary preview framing, low-cost previews, visible-result
priority, immutable media caching, database process ownership, generator aliases,
feature negotiation, and OOM cleanup. The V3 node metadata was useful as a
descriptor-design study, not as a schema to port.

Rejected: node graphs, custom nodes/plugins, its model patcher, and workflow
serialization. Those mechanisms solve extensibility at the cost of the guided,
closed-vocabulary UI this project is built around.

### InvokeAI

Adopted or adapted: append-only migrations and backups, warm-model queue affinity,
VAE working-memory estimation, seam-blended tiles, exact component downloads,
resource lifecycle tests, SSRF principles, and model catalogue separation.

Rejected: tensor-level partial loading, a second model cache/placement owner,
service-locator dependency injection, and full install bundles. PEFT, Diffusers,
Accelerate, and the self-contained setup scripts already own those boundaries.

### SwarmUI

Adopted or adapted: `TrackedParams`, change-cost axis ordering, model-aware
megapixel tiers, schema-driven controls, debounced queue webhooks, and error
guidance that distinguishes absent credentials from refused credentials.

Rejected: arbitrary backend arbitration, accounts, multi-user fairness, graph
workflow generation, and auto-scaling. They add complexity for absent problems.

### krita-ai-diffusion

Adopted or adapted: rectangle/extent vocabulary, regional masked generation,
typed severity bands, backend capability shape, resource identity, application
state persistence, and IPTC provenance semantics.

Rejected: a cloud-provider abstraction, adoption of imported files into the
library, and a dry-run estimator that would duplicate router logic. The live
registry and observed ETA are the single authorities instead.

### SD.Next

Adopted or adapted: architecture probes, quant dispatch with narrow fallbacks,
applied-state idempotency, per-item failure isolation, phase-aware diagnostics,
relative detection filtering, and subsystem-specific logging discipline.

Rejected: its global settings singleton, many overlapping offload systems,
latent correction without per-model validation, and extension/plugin surfaces.

### AUTOMATIC1111

The 58-item deep read was closed as follows:

- Adopted: source-space masked composite, effective-step truth, regional
  inpaint, mask blur/grow, seam-blended tiles, persisted-value validation,
  unified grid axes, materialized seeds, semantic param versions, actual-versus-
  requested metadata, derivative history, metadata import, LoRA sidecars,
  declarative reuse, VRAM peaks, bounded upscaling, resilient face restoration,
  honest progress, numeric grid ranges, robust infotext parsing, atomic writes,
  resolved model identity, aliases, sampler family data, adapter algorithm and
  component weights, collision-free adapter names, directory/search cards,
  load stages, demand-driven previews, prompt sanitization, bounded prompt
  matrices, resize modes, diagnosis, and portable manifests.
- Adapted: post-processing order is fixed but explicit; grids remain interactive
  rather than becoming contact-sheet files; compatibility is visible but known
  mismatches are skipped rather than force-applied; long-prompt handling warns
  instead of owning every encoder; output validation warns rather than running
  an unproven universal fp32 retry.
- Rejected: prompt scheduling, custom denoise loops, variation-seed slerp,
  re-diffused upscale tiles, LAB remapping, training-tag analytics, settings
  mutation per job, automatic server-default rewrites, checkpoint LRUs, meta-
  device eviction tricks, fair multi-tenant locks, process restart controls,
  stack-dump UI, plugin/script lifecycle, CFG++, composable diffusion, and
  filename mini-languages.

Every confirmed defect table was separately rechecked. The actionable defects
now have focused regression tests; contradicted or architecture-inapplicable
claims are retained here only through their Reject rationale.

### Fooocus

Adopted or adapted: Simple/Full disclosure, seized-control explanations,
effective steps, mask-focused inpaint, mask-shaped ramps, exact area-preserving
geometry, metadata dialect detection, field-by-field safe import, LoRA tag
deduplication, raw prompts, bounded wildcard expansion, new-band outpaint masks,
multiscale priming, distilled-regime constraints, tokenizer warnings,
per-variant refine recipes, config completeness/fallbacks, aligned resolution
buckets, portable export, and disk preflight.

Rejected: its specially licensed inpaint algorithm/data, step-varying adapter
weights, reverse-inference of styles from flattened text, Kohya hash as primary
identity, universal `guidance_rescale`, SDXL-only ADM controls, automatic
checkpoint quarantine, and archiving every intermediate. Live previews provide
the useful intermediate feedback without multiplying library assets.

### PurpleDoubleD repository 1191052193

Adopted or adapted: silent-gap watchdogs, warm-up budget, durable remote identity,
Diagnosis wiring, quoted/redacted upstream errors, ordered preflights, cache
completeness, cancellation from any phase, transient retries, live-schema
coercion, work-sized timeouts, hardware probes independent of torch, gated-model
guidance, locked-but-visible states, phase narration, media missing states,
submit rejection versus replay coercion, deterministic test clocks, and
redirect/destination validation principles.

Rejected: killing arbitrary GPU processes, probing foreign ports, installing a
managed Python/ComfyUI runtime, and first-run completion flags. This app owns its
workers and makes diagnosis permanently rerunnable.

### techjarves repository 1258932513

Adopted or adapted: capability-gated offering, unavailable reasons, context-
aware errors, complete-versus-partial artifacts, pre-load validation, honest
fallbacks, authoritative load state, swap visibility, hardware-profile reasons,
write-probe readiness, build stamps, state that survives navigation, and SSRF
protection for the one mutable outbound endpoint.

Rejected: CPU/Vulkan/ROCm/Metal/NPU worker matrix, out-of-process image host,
static-shape NPU special cases, automatic binary installers, and destructive
model lifecycle UI. Its generator lost too many core capabilities to justify a
third lane.

### open-generative-ai

Adopted or adapted: restore-time schema coercion, closed descriptor vocabulary,
step-aware aspect solving, dependent option/range narrowing, separate model
identity, surfaced variant notes, and the rule that custom widgets only present
backend-declared values.

Rejected: hosted-provider pricing/ETA tables, regex/union-find model family
derivation, permissive residual widgets, client-only validation, provider input
blacklists, and the stale 50-record catalogue dump. The repo contained no
generation, memory, or scheduling implementation to transfer.

### Cross-cutting and workstream plans

The original Data, Queue, Backends/Models, Generation, Publishing, Deep-Dive,
Overview, and Owner-Decisions plans were treated as checklists rather than
authority. Their viable work is represented in the matrices above. The major
planned items not built - graph execution, tensor streaming, full model manager,
remote transfer pipeline, and owned denoise/encode loops - have explicit Reject
decisions here instead of remaining ambiguous backlog.

The cross-cutting safety/performance comparison additionally produced explicit
metadata/privacy controls, preservation of repository-provided safety components,
deterministic optional-watermarker behavior, allocator configuration, reclaimable-VRAM math,
true forward warm-up, per-job peaks, serial batch correctness, and the public
content-policy decision.

The LUSTIFY helper was discarded. It converted one private SDXL checkpoint into
one private Hub repository, depended on a write-scoped token, encoded model-
specific scheduler/VAE choices, and was not imported by the app. Configurable
SDXL slots plus family-aware scheduler tuning cover the reusable behavior.

## Validation contract

The implementation is guarded at three levels:

- focused tests named for each mined invariant (geometry, metadata, queue retry,
  cache status, model prefetch, LoRA application, registry dependencies,
  idempotency, export, and config recovery);
- registry/config completeness tests that catch future dead fields and
  undocumented settings; and
- the full `make check` gate: backend tests, Ruff, mypy, frontend tests, ESLint,
  and the production TypeScript/Vite build.

This ledger is the retained artifact. The temporary checkouts and notes are not
required to build, test, run, diagnose, or understand The Wizard's Brush.
