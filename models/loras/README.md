# LoRA adapters

Drop `.safetensors` adapters in this directory and they appear in the LoRA
picker. Nothing else is required — there is no manifest to edit and no restart
needed beyond the catalogue's own cache.

`make models` installs the recommended general-art starter palette for the
default Z-Image Turbo model: pixel art, classic painting, and children's
drawing. Each downloaded adapter has a JSON sidecar recording its Apache-2.0
source, compatible model variant, suggested weight, and trigger guidance.

The weights themselves are gitignored (`models/loras/*`, plus a global
`*.safetensors`); only this file is tracked. That is deliberate: adapters are
multi-gigabyte, they are usually not ours to redistribute, and which ones a
given install holds is a property of that machine rather than of the project.

## How an adapter is matched to a model

Compatibility is decided by **tensor names, not metadata** — see
`backend/app/modelprobe.py`. Trainers stamp stale defaults into
`ss_base_model_version` often enough that it cannot be trusted (a real Flux
adapter here declares `sd_1.5`), so the weights are fingerprinted first and the
declared field is consulted only when the shapes match nothing known.

A family that is not recognised produces "no judgement" rather than "no match",
so an unknown adapter is offered for every model instead of being hidden. If one
of yours is being offered everywhere, its naming scheme probably needs a
fingerprint adding — both the diffusers and the original BFL/ComfyUI namings
have to be covered for a family to be recognised reliably.

## Notes

- The picker only appears for models whose resolved quantization backend can
  merge adapters. INT4 SVDQuant cannot, so the control is scoped per model
  variant rather than per app.
- Adapters are re-applied per generation and always reset first, because
  pipelines share components — a stale adapter would otherwise style the next
  prompt.
- `tests/conftest.py` redirects `LORA_DIR` into a temp root. That redirect is
  what stops `make test` from deleting this directory's real contents.
