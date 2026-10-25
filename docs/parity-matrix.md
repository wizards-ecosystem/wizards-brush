# UI, HTTP and MCP parity

What each surface can do today, row by row (build spec section 10). Each
capability is implemented once in service code; HTTP and MCP are adapters
over it, so a cell marked supported for both runs the same code. "Not yet"
names the packet that adds it. Checked against the tree on 2026-10-24; tests
named in the last column pin the MCP side.

| Capability | UI | HTTP | MCP | Pinned by |
| --- | --- | --- | --- | --- |
| Text to image | Supported (generator form) | Supported (`POST /api/jobs` kind or preset form) | Supported (`generate`, `run_preset`) | `test_mcp_server.py`, `test_mcp_studio.py` |
| Reference / image editing | Supported (Image Edit, Studio reference roles) | Supported (`image_edit`, preset `references`, `inputs.reference_sets`) | Supported (`edit`, `run_preset` with `inputs.references` or `inputs.reference_sets`) | `test_mcp_server.py`, `test_reference_sets.py` |
| Inpaint / outpaint | Supported | Supported | Supported (`edit` with `mask`, kinds `inpaint`, `inpaint_remote`, `outpaint`) | `test_mcp_server.py` |
| Mask import / creation | Supported (mask painter, set mask upload) | Supported (`POST /api/assets/import` role `mask`, `POST /api/variant-sets/masks`) | Supported (`import_mask`; `region_mask` and `matte` kinds through `edit`) | `test_mcp_studio.py` |
| Refine | Supported (asset viewer) | Supported (kind `refine`) | Supported (`edit` with kind `refine`) | `test_job_api.py` |
| Ordered finishing, upscale, background removal | Supported (finishing editor, Tools) | Supported (`finish_steps`, preset `finish`, tool kinds) | Supported (`finish_steps` in `params`, preset `finish`, tool kinds through `edit`) | `test_resolver.py` |
| Video generation and video tools | Supported | Supported | Partial: `t2v` and `long_video` through `generate`, `i2v` through `edit`; `edit` has no `last_frame`, so FLF2V needs HTTP | — |
| Preset create / read / update / archive | Supported (Preset library, Save as preset) | Supported (`/api/studio/presets`) | Supported (`save_preset`, `get_preset`, `update_preset`, `archive_preset`, `clone_preset`) | `test_mcp_studio.py` |
| Resolve / run / replay a preset | Supported (Studio) | Supported (preset form of `POST /api/jobs`, `/api/jobs/{id}/replay`) | Supported (`resolve_preset`, `run_preset`, `replay_job`, `get_plan`); same hashes as HTTP | `test_mcp_studio.py` |
| Reference-set management | Supported (Reference sets page, Studio role pickers) | Supported (`/api/reference-sets`) | Supported (`list_reference_sets`, `get_reference_set`, `save_reference_set`, `update_reference_set`, `archive_reference_set`) | `test_mcp_studio.py`, `test_reference_sets.py` |
| Saved-generator check / run | Supported (Recipes) | Supported (`/api/recipes/{key}/run`) | Supported (`get_recipe`, `run_recipe`) | `test_mcp_server.py` |
| Variant Set preview / create / status / cancel / retry / rerun / export | Supported (Variant Sets pages, preset stages) | Supported (`/api/variant-sets/*`, stage `preset`) | Supported (`preview_variant_set`, `create_variant_set`, `wait_variant_set`, `cancel_variant_set`, `retry_variant_set`, `rerun_variant_item`); ZIP export over HTTP, sheets and atlases over MCP too | `test_mcp_studio.py`, `test_variant_sets_presets.py` |
| Sprite sheet / atlas export | Supported (Variant Set detail, Sheet / atlas) | Supported (`/api/variant-sets/{id}/sprites[/preview]`, `/api/artifacts/{id}[/file]`) | Supported (`preview_sprite_export`, `create_sprite_export`, `get_artifact`, `fetch_artifact`) | `test_sprite_export.py`, `test_spritesheet.py` |
| Result search / inspect / download | Supported (Gallery) | Supported (`/api/assets`, `/api/assets/{id}/file`) | Partial: `search_library`, `get_job`, `brush://assets/{id}`, `fetch_asset` for images up to 16 MB; videos and larger files only through `file_url` | `test_mcp_studio.py` |
| Job cancellation | Supported | Supported (`/api/jobs/{id}/cancel`) | Supported (`cancel_job`, which separates requested from settled) | `test_mcp_studio.py` |
| Targets and routing policy | Supported (Settings → Routing, Remote GPU panel) | Supported (`/api/targets`, `PUT /api/targets/policy`) | Read-only by design (`list_targets`): an agent cannot widen where inputs may go | `test_mcp_studio.py` |
| Resources: inventory, local import, provider import, remote preparation | Supported (Resources page) | Supported (`/api/resources/*`); provider imports with P07 | Partial: `list_resources`, `get_resource`, `rescan_resources`, `prepare_resource_on_remote`; no file upload over MCP | — |
| Canvas document operations | Not yet (P20) | Not yet (P20) | Not yet (P20) | — |

## Client classes

| Client | Supplies images | Retrieves results |
| --- | --- | --- |
| A local coding client with a shell (Claude Code, Cursor) | Asset ids from `POST /api/assets/import`, or `data:` URLs to `edit` and `import_mask` | `GET /api/assets/{id}/file` with the `X-API-Token` header |
| A no-shell, browser-style MCP client | `data:` URLs to `edit` and `import_mask` | `fetch_asset` (images up to 16 MB), or `inline_images` for a 512 px preview |

Network reachability and the API token are installation requirements: an
unreachable server or a 401 is not a failed generation. Body size, host and
origin checks are the same for MCP as for the rest of `/api`.
