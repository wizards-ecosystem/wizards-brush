# User guide

The Wizard's Brush is organized around a simple loop: choose a process, queue
work, inspect the result, and reuse what worked. The Simple form shows the safe
core controls; Full controls exposes the model-specific surface.

## Make a first image

1. Open **Studio** and choose **New image** under **From a description**.
2. Write what should be visible, including subject, setting, light, and medium.
3. Keep the default size and quality for the first run.
4. Submit the job, then follow its stage and preview in **Queue**.
5. Open the result in **Gallery** to compare, favorite, tag, export, or reuse it.

A seed makes a workflow repeatable only when the model and effective settings
also match. Reuse from the asset inspector restores the recorded values; values
that no longer exist are surfaced rather than silently substituted.

## Start from an image

The launcher groups workflows by what you already have:

- **Remix an image** changes the whole composition while preserving influence from the source.
- **Replace part of an image** uses a painted mask; only the selected region is regenerated.
- **Extend past the edges** adds canvas around the source and composites the original pixels back in.
- **Edit by instruction** sends one to three reference images to the configured Remote GPU editor.
- **Copy a pose or composition** uses local ControlNet when its optional dependencies and safe device settings are enabled.
- **Animate an image** uses the selected Remote GPU video engine.

## Prompts, presets, and wildcards

Use `{red|blue|green}` for an inline choice. Use `__lighting__` or another
double-underscore name to sample from a file in `wildcards/`. Manage those files
from **Settings -> Wildcards**.

Save useful prompt fragments as presets instead of copying entire workflows.
X/Y Grid can compare prompt text or numeric controls across a repeatable matrix.
Grid cells retain their effective parameters and seed derivation.

## Queue behavior

Local and remote work use separate lanes, so network-bound remote jobs do not
block the local GPU. Local GPU operations are serialized to protect VRAM. You
can reorder queued work, cancel a running job cooperatively, skip queued work,
or rerun a completed job.

Queued jobs survive a normal restart and are resumed. Jobs that were actively
running are canceled during reconciliation because their original handler no
longer exists.

## Gallery and files

The gallery supports text search, captions, tags, favorites, ratings,
collections, comparisons, and soft deletion. Trash hides an asset everywhere
else; permanent purge is the only operation that removes its file.

Generated files can embed two independent records:

- **Generation settings** - prompt, seed, model, adapters, and effective workflow values for replay.
- **AI provenance** - a standardized IPTC declaration that AI contributed to the pixels, without exposing the prompt.

Change these defaults in **Settings -> File metadata** before generating files
that will leave the private library. Disabling generation metadata also removes
it from future ZIP manifests and suppresses video sidecars; the private database
record remains available for library workflows. Existing PNG files are not
rewritten or scrubbed.

## Models and adapters

The model picker only shows configured slots. Switching large local or Remote
GPU models evicts the resident pipeline, so the first job after a switch can
include a long load. Queue affinity groups compatible jobs to avoid needless
back-and-forth swaps.

Place `.safetensors` LoRA adapters in `models/loras/`. Pickle-backed `.pt` and
`.bin` adapters are not catalogued or loaded because they can execute publisher-
controlled Python. Convert a legacy adapter only in an isolated, trusted
environment, then review its license and provenance before using the result.

## Finishing tools

The **Finish** preset expands into recorded detail, face-restoration, and
upscale steps. These operations are explicit and appear in the saved metadata.
Upscaling is tiled and local CPU work is capped so finishing does not monopolize
the workstation.

## Remote GPU

Remote generation is optional and uses a worker on hardware you operate or are
authorized to expose. The tunnel URL is not an authentication boundary; the
shared secret and host network policy are. Follow the complete
[Remote GPU guide](remote-gpu.md) before enabling the lane.

## Privacy and responsibility

Prompts, uploads, captions, generated media, and metadata stay in this
checkout unless you configure a Remote GPU or notification URL. A Remote GPU
receives the inputs required for its jobs. Notification endpoints receive the
documented event payload.

You are responsible for the media you provide and create, the rights and terms
attached to models and adapters, and the law that applies to your use. The
application does not grant rights to third-party training data, input media,
model weights, or generated likenesses.
