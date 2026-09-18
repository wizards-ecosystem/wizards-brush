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

A failed job is announced once, when it fails; reloading the page does not
announce it again. The jobs of a variant set are summarised by their set
instead: one notice when the set settles, with its counts.

## Variant sets

A variant set takes approved source material, varies it along named axes, and
tracks every combination as its own job: one object in several materials, one
layout in several locales, one scene under several lighting setups. Open it from
**Variants** in the navigation. X/Y Grid is unchanged and remains the tool for a
parameter study of one prompt; "variant" here is unrelated to the model picker.

### Recipes and sets

- A **recipe** is a reusable definition: the operation, axes, templates,
  settings, finishing, validation and output names. Save one from the editor
  with **Save as recipe**, and start new sets from it under **Saved recipes ->
  Use**. Editing a recipe never changes a set already made from it.
- A **set** is one execution: a frozen snapshot of the recipe plus one item per
  combination, each with its own state, job, output and validation.
- On a set's page, **Duplicate as new set** opens its recipe in the editor to
  run again, changed or not; **Save as recipe** keeps it for later; **Delete
  set** removes the set record once it is no longer running. Its images and
  jobs stay in the library either way.

### Axes and the count

An axis has a name (letters, digits and `_`, starting with a letter) and a list
of values. A stage's count is the product of its axes' value counts: axes of 3,
2, 4 and 2 values make 3 x 2 x 4 x 2 = 48 variants. A stage with no axes runs
once; an axis with no values makes the count 0. The editor shows the count as
you type.

Every set is checked against `VARIANT_MAX_COMBINATIONS` (default 1000), counted
across every stage, before anything is queued. A set over the limit is refused
with its count; combinations are never silently dropped. Values that differ only
by case, spacing or punctuation (`Dark grey`, `dark-grey`) are one value to a set
and are refused, because their variants could not be told apart by name.

### Templates

Write the instruction once, for example
`Render the object in {{finish}} {{color}} {{material}}, {{lighting}}`, and
click an axis chip to insert its placeholder. **Describe each value** maps a
value to longer text: with `soft -> soft even neutral studio illumination`,
`{{lighting}}` renders the description and `{{lighting.value}}` renders `soft`.

Templates are checked before anything runs. An unknown name, an axis that only
a later stage defines, and a stray `{{` are errors. Substitution happens once:
a description is inserted as written and never expanded again. The prompt
engine's own `{a|b}` choices and `__wildcard__` files keep their usual meaning.

### A one-stage reference-driven edit

1. **Variants -> New variant set**, then choose **Image Edit**.
2. **From gallery** picks a reference image (up to three); **Upload** brings
   one in from this computer, keeping its transparency. Every variant uses the
   same references; only the instruction and settings vary.
3. Add axes and values, and write the instruction template.
4. Adjust the operation's own settings: the same registry controls its
   generator page offers, including its Finishing preset.
5. Optionally add finishing steps, validation checks, an output-name template,
   and a new collection for the results.
6. Check the preview (every rendered instruction and output name), then
   **Create set**.

Img2img and outpaint take one source image; text-to-image takes none; ControlNet
appears when it is enabled. Inpaint takes one source plus a mask: **Select the
subject** or **Select the background** makes one in a click (the background
matte below, run on this machine), or paint it over the source. Masks are kept
as reusable gallery assets.

### Staged derivation

**Add a derived stage** runs another operation on every output of the stage
before it, with its own axes, template and settings, and access to the earlier
stages' axes. A variant's key accumulates across stages
(`material=wood,angle=front`). A derived Image Edit stage can also send up to
two fixed **reference images** after its source, the same for every variant of
that stage. A derived variant waits until its source succeeds. If the source fails, is invalid or is canceled, the variant is
**blocked** and does not run; retrying the source unblocks it. Stages are an
ordered list, not a graph.

### Seeds

**Shared seed** gives every variant the same seed, so variants differ only in
what their axes change. **Seed per variant** derives each variant's seed from the
base seed and its key, so it stays stable when values are added or reordered. A
base seed of -1 is resolved once, when the set is created, and recorded.

### Finishing

The operation's own Finishing preset runs as usual. A set can add an ordered
list of steps that run inside each variant's job, after the preset, in the
order shown; move a step up or down to change it. The same list is available to
any single image generation under **Full controls -> Advanced controls ->
Finishing steps**.

- **Remove background** writes a real PNG alpha channel at the original size
  using BiRefNet-lite (MIT; a 224 MB weight downloaded on first use). Pixels
  are never composited onto a checkerboard or a colour. It needs an image with
  a clear subject: a scene without one can come back almost entirely
  transparent, which a maximum-transparency check catches.
- **Resize to an exact size**: contain (with transparent or colour padding),
  cover, or stretch.
- **Upscale** (Real-ESRGAN, 2x or 4x) and **Restore faces** (GFPGAN), which
  carry transparency across.

A step that the installation cannot run is named with the reason instead of
being offered.

A step that cannot run keeps the image from before it and records a warning on
the asset, and the set marks that variant **invalid**: what was asked for did
not happen.

### Validation

Each finished file can be checked for: a readable file, PNG format, an exact
size, alpha required or forbidden, transparent corners, a minimum and maximum
transparent percentage, and a safe margin around the visible content. Margins
are measured from real transparency; on an opaque image the check reports a
warning rather than a guess. A failed check marks the variant **invalid**; a
warning keeps it successful. Every result and its details are shown per variant.

### Progress, retry and rerun

The set page counts expected, done, running, queued, waiting, failed, invalid,
blocked and canceled variants - for example 48 expected, 46 done, 1 failed,
1 invalid - and lists every combination with its values, output, state, reason
and validation.

- **Retry failed** reruns only failed and invalid variants, from the exact
  parameters that failed. Successful variants are never touched.
- **Cancel remaining** stops queued and waiting variants; a running one stops
  at its next step. Finished variants keep their results, and **Resume
  canceled** picks the rest up again.
- **Rerun** and **New seed** on a row run one variant again on purpose, even a
  successful one. Earlier attempts stay in its history. In a staged set a rerun
  leaves what later stages made from it as it is; **+ later stages** reruns the
  variant and then everything derived from it.
- **Retry** on the Queue page for a failed child retries its variant, so the set
  keeps tracking it.

A derived stage shows what each variant was made from, and a long stage shows
100 rows at a time.

### Names and export

Output names come from a template over the axes: `{{material}}/{{color}}_{{finish}}`
gives `wood/red_matte.png`. `{{axis}}` is the value's identifier (lower case,
hyphenated), `{{axis.value}}` the value as written, `/` makes a folder, and the
extension comes from the file. A blank template uses every axis and cannot
collide. A template that would give two variants one name is refused before
anything runs, and names cannot leave the export folder.

**Export ZIP** holds every successful output of the final stage under its name,
plus `wizards-brush-manifest.json`: the set and its recipe snapshot, and for each
file its key, axis values, output name, asset id, source lineage, operation,
validation and - unless generation metadata is off in **Settings -> File
metadata** - model, prompt, seed and settings. Variants that were not exported
are listed with their state and reason. Library files keep their own unique
names; export names apply only inside the archive.

### Restarts

Sets survive restarts. Queued variants resume on their lanes. A variant that was
running is canceled with "interrupted by restart" and can be retried. A variant
that finished while the app was stopping is settled at startup, and a derived
stage continues from it.

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
the workstation. Any image generation and every variant set stage can add
further named steps, such as background removal to a real PNG alpha channel and
resizing to an exact canvas; they are recorded in the same processing history.

**Tools -> Background** runs the background matte (BiRefNet-lite, MIT) on an
image you already have: **Cut out the subject** saves a transparent PNG, and
**Mask the subject** or **Mask the background** saves a mask (white = change)
for inpainting or a variant set. Viewers show transparency on a checkerboard.

## Scripting and the API

Everything here is also available over HTTP, and the interface is one client of
that API. The [API guide](api.md) covers bringing images in, queuing any
generator or tool, waiting for results and running variant sets from a script.

To see the request for what is on screen, use **Copy as an API request** (the
copy icon at the top of any generator's panel) or **Copy API request** in the
variant set editor. Each copies a runnable `curl` command.

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
