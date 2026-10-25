# Studio contracts

The vocabulary for presets, resources, execution plans and the work built on
them. Names here are decided once; code imports them from
`backend/app/studio/contracts.py` rather than spelling its own. Fixtures live in
`tests/fixtures/studio/` and `tests/fixtures/legacy/`, and
`tests/test_studio_contracts.py` pins them.

## Baseline

The build specification was reviewed against commit `e844fac` ("Saved recipes:
callable job kinds for UI, API and MCP", #42). Implementation started from that
exact commit; the local `main` was fast-forwarded to it and no commit lies
between the two. Every static finding in specification section 11 was checked
by reading the code at that commit:

| Finding | Where | At `e844fac` |
| --- | --- | --- |
| Stop clears the session in `finally` | `provisioners/runpod.py` `stop()` | Reproduced: a timeout, 403 or 5xx loses the only adoption handle |
| Session panel stops polling once ready | `RemoteGpuControl.tsx` | Reproduced: the interval runs only in `starting`/`stopping` |
| `legalValue` has no finishing branch | `frontend/src/lib/generators.ts` | Reproduced: a `finish_steps` list restores as the default and is reported stale |
| Style chips are not frozen into saved generators | `GeneratorView.tsx`, `SaveRecipeDialog.tsx` | Reproduced: Generate composes style text; Save keeps only registered controls |
| Ctrl/Cmd+Enter is a window listener | `GeneratorView.tsx` | Reproduced: it fires under any dialog |
| A late source fetch overwrites a newer one | `GeneratorView.tsx` | Reproduced: no abort and no latest-request check |
| Controls come from a boot-time fetch | `store/useStore.ts` | Reproduced: `/api/models` is read once; only `/api/system` polls |
| A missing variant falls back; a missing LoRA is dropped | `generators/variants.py` `get()`, `loras.py` `sanitize()` | Reproduced; kept for legacy jobs, refused for strict plans |
| Image builders normalize after validation | `routers/images.py` `common_params` | Reproduced: speed mode, quality, aspect, batch and remote adapters |
| Remote backend facade is unimplemented | `backends/remote_gpu.py` | True, and irrelevant to rendering: `remote_gpu_client.run_remote` is the real lifecycle |

## Inventory at the baseline

**Persisted job kinds** (`models.JobKind`, never renamed): `image_local`,
`image_colab`, `image_edit`, `img2img`, `inpaint`, `inpaint_remote`, `outpaint`,
`control_local`, `t2v`, `i2v`, `long_video`, `extend_video`, `upscale`,
`face_restore`, `interpolate`, `detail`, `matte`, `region_mask`. The local lane
runs the first five image kinds, `control_local` and the local tools
(`queue._LOCAL_KINDS`); everything else is the remote lane.

**Stored setups.** Three things are called a recipe or a preset, and they are
not interchangeable:

| Name | Table / module | Meaning |
| --- | --- | --- |
| Saved generator (v1) | `recipe`, `recipes.py` | One job kind, frozen params, a narrow exposed-input schema; runs through `POST /api/jobs` |
| Variant Set recipe | `variantrecipe`, `variant_sets/recipe.py` | Staged expansion across axes; sets snapshot it |
| Quick-setting bundle | `presets.py` `SETTING_PRESETS` | A few control values applied in the form; leaves model, prompt, aspect and seed alone |

**Migrations.** Head is `0011_recipe_defaulted`; the runner refuses a database
that records an id it does not know.

**HTTP.** Every route is under `/api`: generation (`/generate/*`), the job API
(`/jobs`, `/jobs/kinds`, `/jobs/{id}/wait`, cancel/skip/front/move/rerun, the
job WebSocket), saved generators (`/recipes`), Variant Sets (`/variant-recipes`,
`/variant-sets/*`), assets and library, tools (`/tools/*`), system and
registry (`/system`, `/models`, `/presets`, `/backends`), settings, LoRAs,
wildcards, collections, Remote GPU sessions (`/remote-gpu/session`), MCP
(`/api/mcp`) and the OpenAI image API (`/api/openai/v1`).

**MCP tools.** `list_kinds`, `describe_kind`, `generate`, `edit`, `get_job`,
`wait_job`, `search_library`, `list_recipes`, `get_recipe`, `save_recipe`,
`run_recipe`; resources `brush://kinds[/{kind}]`, `brush://recipes[/{key}]`,
`brush://jobs/{id}`, `brush://assets/{id}`. Missing at the baseline: cancel, set
orchestration, preset and resource discovery.

**Remote lifecycle.** The worker exposes `POST /image|/edit|/inpaint|/t2v|/i2v`
(submit, returns a token), `GET /result/{token}`, `POST /ack/{token}`,
`POST /cancel/{token}`, `POST /cancel/client/{client_id}` and `GET /health`.
`remote_gpu_client.run_remote` drives it with bounded retries and records
`remote_token`/`remote_client_id` on the job so a restart can reclaim the work.
The worker queue and result cache are in memory: queued local work survives a
restart, and an interrupted diffusion process is not resumed.

## Document families

`contracts.classify()` decides which family a document is from the fields each
family must have:

* **Saved generator v1**: has a job `kind`, no `schema_version` above 1.
  Unchanged semantics; every existing row stays one.
* **Preset v2**: `schema: "wizards-brush/preset"`, `schema_version: 2` and an
  `operation`. A document that names both an operation and a `kind` is refused
  as `ambiguous_request`.
* **Variant Set recipe**: has `stages` or `axes`.

## Presets (schema v2)

A `PresetDefinition` holds an author's values, never compiled ones: stable `id`
(optionally one `/` namespace, `curated/…` for shipped entries), `revision`,
`name`, `description`, `origin` (`curated` or `custom`), `tags`, `operation`,
`model.resource`, ordered `adapters` with strengths, `prompt` (template,
negative, declared style fragments, default styles, enhancement modifiers),
scalar `settings` named like registry controls, ordered `finish` steps,
reference `roles`, a validator `validation` policy, editable `fields` help, and
`curation`.

* **Origin is ownership, not quality.** An AI-authored preset saved through MCP
  is `custom`; who wrote it is recorded as the client tag. A `curated` entry must
  carry evidence, tested targets and limitations; `curation.status` stays
  `unreviewed` until a person reviews the evaluation pack.
* **Order is meaning.** Adapters and finishing steps are never sorted, and their
  order is part of the definition hash.
* **Settings are validated by the registry.** Types, bounds, visibility and
  dependent defaults come from the control descriptors (`controls.py`), at
  resolve time against the live kind. Structured values (adapters, finishing,
  references) have their own fields.
* **Templates** use the Variant Set grammar with a fixed set of placeholders:
  `{{prompt}}` (the caller's subject prompt), `{{styles}}` (selected fragments,
  in declaration order), `{{triggers}}` (adapter trigger guidance, in adapter
  order) and `{{notes}}` (reference-set notes). There are no filters,
  expressions or attribute access, and single braces stay literal so `{a|b}`
  choices and `__wildcards__` keep working.

**Revisions.** A curated revision is immutable: editing one makes a custom copy.
Saving a custom preset writes a new revision; a save names the revision it
started from and is refused if another save got there first. Deleting archives;
revisions referenced by jobs are kept.

**Storage.** A preset is a `recipe` row with `schema_version` 2; the row is the
catalogue head and `reciperevision` holds every revision. Curated revisions
ship as `backend/app/studio/curated/<name>.r<revision>.json`, one file per
revision, checked against `LOCK.json` at startup: a shipped revision edited in
place is refused, and adding one means a new file and a new lock entry
(`catalogue.lock_entry`). A curated preset that stops shipping is archived.

## Resources

A resource id is the provider identity plus an exact version:

| Provider | Id |
| --- | --- |
| Hugging Face | `hf:<repo>@<commit>`, or `hf:<repo>@unpinned` for a custom slot that follows the configured identifier |
| Civitai | `civitai:model/<id>/version/<id>/file/<id>` |
| Local import | `local:sha256:<64 hex>` (content addressed) |
| Shipped code | `builtin:<name>` |

Storage locations are installations of a resource, never its identity. An
install's location is private: `<root>:<relative path>` under a managed root
(`loras`, `weights`) or `hf:<repo>` for a Hub cache snapshot, resolved with a
containment check that refuses traversal and symlinks leaving the root.
Lifecycle states are `known`, `access_required`, `downloading`, `installed`,
`verified`, `runnable`, `unavailable` and `failed`. Verification is separate:
`legacy_unverified` (registered from an older install, never hashed),
`unverified`, `verified` and `mismatch`. A curated recommendation, structural
compatibility, a test on a target and current readiness are four facts and are
never folded into one flag.

## Operations, targets and kinds

A plan maps a logical operation and a resolved target onto one persisted kind
(`contracts.KIND_FOR`):

| Operation | Local | Remote |
| --- | --- | --- |
| `text_to_image` | `image_local` | `image_colab` |
| `image_to_image` | `img2img` | — |
| `inpaint` | `inpaint` | `inpaint_remote` |
| `outpaint` | `outpaint` | — |
| `edit` | — | `image_edit` |
| `control` | `control_local` | — |
| `text_to_video`, `image_to_video`, `long_video` | — | `t2v`, `i2v`, `long_video` |

A dash means the target does not offer that operation today, and the resolver
says so instead of borrowing the other target's kind. Targets requested are
`local`, `remote` or `auto`; a plan records the resolved one and why.

## Execution plans and hashes

A plan is the immutable effective request captured before submission. Every
value carries its origin: `registry_default`, `dependent_default`, `preset`,
`override`, `imported` or `runtime`. A model change recomputes only the first
three; an explicit value equal to today's default stays `override`.

Three hashes, each over canonical JSON (sorted keys, integral floats as
integers) with a domain tag so they can never collide:

* **definition hash**: one preset revision's authored content.
* **creative_request_hash**: operation, resource identities, effective prompts
  and settings, references and masks, seed and finishing. It excludes the
  target, transport ids and timestamps, so the same fixed-seed request through
  the UI, HTTP and MCP hashes the same.
* **execution_plan_hash**: the creative hash plus the resolved target identity,
  the concrete kind, the expected runtime and the resource and capability
  revisions. Submission compares it to refuse a plan that went stale.

A third, the **material_hash**, is the execution hash without the capability
revision. Submission compares it to tell a change in what would run (stale)
from a change in what the target offers that leaves this plan alone.

Observation timestamps are in neither. The runtime that actually executed is
recorded on the job afterwards; a mismatch with the expected runtime is
reported, never hidden under a strict-replay claim.

## Migration ids

Ids are assigned here, centrally, as each packet lands. Taken:
`0012_recipe_styles` (P02, frozen style fragments on saved generators) and
`0013_recipe_revisions` (P03: `schema_version`, `preset_id`, `revision`,
`origin`, `definition_json`, `definition_hash` and `archived_at` on `recipe`,
the append-only `reciperevision` table, and name uniqueness scoped to saved
generators), `0014_resources` (P06: `resource` and `resourceinstall`),
`0015_execution_plans` (P05: the `executionplan` table; `plan_id`,
`request_digest`, `backend_id` and `execution_json` on `job`) and
`0016_resource_imports` (P07: the `resourceimport` task table) and
`0017_reference_sets` (P14: `referenceset` and the append-only
`referencesetrevision`) and `0018_artifacts` (P16: the `artifact` table for
typed export outputs). Planned: `0019_canvas_projects`. A packet
that lands out of order takes the next free number and updates this list. Every migration is additive: new columns are
nullable or have a safe historical default, and no migration hashes weights or
downloads anything.
