import type { Control, GeneratorSpec } from "../api/types";

/** Asset.generator → generator spec id (persisted strings; keep legacy keys for old DB rows). */
export const GEN_TO_SPEC: Record<string, string> = {
  "local_image:txt2img": "image_local",
  "local_image:img2img": "img2img",
  "local_image:inpaint": "inpaint",
  "local_image:outpaint": "outpaint",
  "local_image:control": "control_local",
  "local_flux:txt2img": "image_local", // pre-rename rows
  "local_flux:img2img": "img2img",
  "local_flux:inpaint": "inpaint",
  colab_a100: "image_colab",
  colab_edit: "image_edit",
  "colab_wan:t2v": "t2v",
  "colab_wan:i2v": "i2v",
  long_video: "long_video",
};

/** Tool job kinds that run on the local GPU/CPU (no generator spec of their own). */
export const LOCAL_TOOL_KINDS = new Set(["upscale", "face_restore", "interpolate", "detail"]);

/** Params keys that are server-side paths/blobs — never render or prefill them. */
export const HIDE_KEYS = new Set([
  "image_path",
  "mask_path",
  "src_path",
  "src_meta",
  "image_paths",
  "last_image_path",
  "grid",
  // A record of what was done to the image after generation, not an input.
  // Prefilling it would put a result back in as a request.
  "post",
  "source_asset_id",
  "src_width",
  "src_height",
]);

export function laneOf(kind: string, specs: GeneratorSpec[]): "local GPU" | "A100" {
  const spec = specs.find((s) => s.id === kind);
  if (spec) return spec.needs_remote ? "A100" : "local GPU";
  return LOCAL_TOOL_KINDS.has(kind) ? "local GPU" : "A100";
}

/** Any job whose kind matches a generator spec can be re-run by id. */
export function isRerunnable(kind: string, specs: GeneratorSpec[]): boolean {
  return specs.some((s) => s.id === kind);
}

/** Strip non-prefillable keys from stored job params for "edit queued job" / reuse. */
export function prefillableParams(params: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = {};
  Object.entries(params || {}).forEach(([k, v]) => {
    if (!HIDE_KEYS.has(k)) out[k] = v;
  });
  // New rows carry both: prompt is exactly what ran, raw_prompt is what the
  // person typed before style templates were composed. A form wants the latter
  // or selecting the same style again duplicates it.
  if (typeof params?.raw_prompt === "string") out.prompt = params.raw_prompt;
  delete out.raw_prompt;
  return out;
}

/** Map an asset's stored meta back onto generator form values. */
/** Largest side any generator will actually produce (the A100 lane caps at
 *  1664 and snaps to /16). Beyond this, stored dimensions describe a
 *  post-processed file rather than a generation request. */
export const MAX_REUSE_SIDE = 1664;

export function reuseValues(meta: Record<string, any>): Record<string, any> {
  const v: Record<string, any> = {};
  const copyKeys = [
    "prompt",
    "negative_prompt",
    "seed",
    "guidance",
    "steps",
    "strength",
    "num_frames",
    "fps",
    "orientation",
    "resolution",
    "model_variant",
    "sampler",
    "finish",
    "style_ids",
    // Adapters are part of what produced the image, so "reuse all" has to carry
    // them; without this the remix silently drops the style.
    "loras",
  ];
  copyKeys.forEach((k) => {
    if (meta[k] != null) v[k] = meta[k];
  });
  if (typeof meta.raw_prompt === "string") v.prompt = meta.raw_prompt;
  if (meta.steps != null) v.quality = "Custom";

  // Only reuse explicit dimensions that are plausibly GENERATION dimensions.
  //
  // Assets created before post-processing was recorded stored their upscaled
  // size here — a x4 upscale of 672x896 was saved as 2688x3584. Feeding that
  // back as a request asks the model for an image 16x the area it actually
  // made: far slower, differently composed, and nothing like a "reuse".
  // Anything past MAX_REUSE_SIDE is treated as post-processed, and the recorded
  // aspect is used instead.
  // New derivative records carry both domains explicitly. Prefer the ancestor's
  // diffusion size; the current file may be a 4K upscale. The cap remains only
  // as backwards-compatible protection for rows saved before the split.
  const recipeWidth = meta.generation_width ?? meta.width;
  const recipeHeight = meta.generation_height ?? meta.height;
  const hasExplicitRecipe = meta.generation_width != null && meta.generation_height != null;
  if (
    recipeWidth &&
    recipeHeight &&
    (hasExplicitRecipe || (recipeWidth <= MAX_REUSE_SIDE && recipeHeight <= MAX_REUSE_SIDE))
  ) {
    v.aspect = "Custom";
    v.width = recipeWidth;
    v.height = recipeHeight;
  } else if (meta.aspect && meta.aspect !== "Custom") {
    v.aspect = meta.aspect;
  }
  return v;
}

export interface CoercedValues {
  values: Record<string, any>;
  adjusted: string[];
}

export function effectiveControlDescriptor(control: Control, values: Record<string, any>): Control {
  const dependent = control.overrides_by;
  const override = dependent?.map[String(values[dependent.field])];
  return override ? { ...control, ...override } : control;
}

function legalValue(control: Control, raw: any): { value: any; adjusted: boolean } {
  if (raw === undefined || raw === null) return { value: control.default, adjusted: false };
  if (["select", "segmented", "aspect"].includes(control.type)) {
    return control.options?.includes(String(raw))
      ? { value: String(raw), adjusted: false }
      : { value: control.default, adjusted: true };
  }
  if (control.type === "slider" || control.type === "number") {
    const n = Number(raw);
    if (!Number.isFinite(n)) return { value: control.default, adjusted: true };
    const bounded = Math.max(control.min ?? -Infinity, Math.min(n, control.max ?? Infinity));
    return { value: bounded, adjusted: bounded !== n };
  }
  if (control.type === "toggle") {
    return typeof raw === "boolean"
      ? { value: raw, adjusted: false }
      : { value: control.default, adjusted: true };
  }
  if (control.type === "lora") {
    return Array.isArray(raw) ? { value: raw, adjusted: false } : { value: control.default, adjusted: true };
  }
  if (control.type === "textarea") {
    return typeof raw === "string"
      ? { value: raw, adjusted: false }
      : { value: control.default, adjusted: true };
  }
  return { value: control.default, adjusted: raw !== control.default };
}

/** Restore untrusted draft/history/meta values against the live schema.
 * Dynamic model and capability lists change normally; stale state is therefore
 * expected and is snapped or dropped before it can make the form lie. */
export function coerceValues(
  controls: Control[],
  raw: Record<string, any> | null | undefined,
  validStyleIds?: Set<string>,
): CoercedValues {
  const input = raw || {};
  const values: Record<string, any> = {};
  const adjusted = new Set<string>();
  const names = new Set(controls.map((c) => c.name));

  controls.forEach((control) => {
    const result = legalValue(effectiveControlDescriptor(control, input), input[control.name]);
    values[control.name] = result.value;
    if (result.adjusted) adjusted.add(control.label);
  });

  // A dependent default follows the final, legal controller when its own value
  // was absent. If the controller itself was invalid, do not carry that stale
  // model's guidance/step regime onto the fallback model.
  controls.forEach((control) => {
    const dep = control.defaults_by;
    if (!dep) return;
    const controller = controls.find((c) => c.name === dep.field);
    const controllerChanged =
      controller && input[dep.field] !== undefined && values[dep.field] !== input[dep.field];
    const mapped = dep.map[String(values[dep.field])];
    if (mapped !== undefined && (input[control.name] === undefined || controllerChanged)) {
      if (values[control.name] !== mapped && controllerChanged) adjusted.add(control.label);
      values[control.name] = mapped;
    }
  });

  const rawStyles = Array.isArray(input.style_ids)
    ? input.style_ids.filter((x) => typeof x === "string")
    : [];
  values.style_ids = validStyleIds ? rawStyles.filter((id) => validStyleIds.has(id)) : rawStyles;
  if (values.style_ids.length !== rawStyles.length) adjusted.add("Styles");

  Object.keys(input).forEach((name) => {
    if (!names.has(name) && name !== "style_ids" && !HIDE_KEYS.has(name)) adjusted.add(name);
  });
  return { values, adjusted: [...adjusted] };
}

export function effectiveControlValue(control: Control, values: Record<string, any>): any {
  const constraint = control.constrained_by;
  if (constraint) {
    const got = values[constraint.field];
    const matches = Array.isArray(constraint.equals)
      ? constraint.equals.includes(got)
      : got === constraint.equals;
    if (matches) return constraint.value;
  }
  return values[control.name] ?? control.default;
}

export function effectiveControlDefault(control: Control, values: Record<string, any>): any {
  const forced = effectiveControlValue(control, { ...values, [control.name]: undefined });
  if (control.constrained_by && forced === control.constrained_by.value) return forced;
  return control.defaults_by?.map[String(values[control.defaults_by.field])] ?? control.default;
}

/**
 * Re-point controls whose sensible default depends on another field.
 *
 * The local lane serves two models with incompatible CFG regimes — Z-Image-Turbo
 * is distilled around CFG 1.0, Z-Image base is a real-CFG model wanting ~4.0 —
 * so switching `model_variant` must move the guidance slider with it. It only
 * moves a value the user has not touched: "untouched" means the value still
 * equals the default mapped for the variant we are leaving. A hand-set number
 * is always kept.
 *
 * Returns only the keys that should change, so the caller can merge.
 */
export function applyDependentDefaults(
  controls: Control[],
  changedField: string,
  prevValue: any,
  nextValue: any,
  values: Record<string, any>,
): Record<string, any> {
  const out: Record<string, any> = {};
  for (const c of controls) {
    const dep = c.defaults_by;
    if (!dep || dep.field !== changedField) continue;
    const nextDefault = dep.map[String(nextValue)];
    if (nextDefault === undefined) continue;

    const current = values[c.name];
    const prevDefault = dep.map[String(prevValue)];
    const untouched = current === undefined || current === null || current === prevDefault;
    if (untouched) out[c.name] = nextDefault;
  }

  // A dependent range is not a default: preserve the user's value whenever it
  // remains legal, and reconcile it only when the newly selected regime cannot
  // represent it. This makes an LTX frame count become a visible legal Wan
  // count at the moment of the switch instead of being silently changed later.
  const prospective = { ...values, ...out, [changedField]: nextValue };
  for (const c of controls) {
    if (c.overrides_by?.field !== changedField) continue;
    const descriptor = effectiveControlDescriptor(c, prospective);
    const raw = out[c.name] ?? values[c.name] ?? descriptor.default;
    const legal = legalValue(descriptor, raw).value;
    let reconciled = legal;
    if (
      (descriptor.type === "slider" || descriptor.type === "number") &&
      descriptor.step &&
      Number.isFinite(descriptor.min)
    ) {
      const min = descriptor.min as number;
      // Use the lower legal step, matching the server's 4k+1 Wan clamp.
      const steps = Math.floor((Number(legal) - min + Number.EPSILON) / descriptor.step);
      reconciled = min + Math.max(0, steps) * descriptor.step;
      if (descriptor.max != null) reconciled = Math.min(reconciled, descriptor.max);
    }
    if (reconciled !== raw) out[c.name] = reconciled;
  }
  return out;
}
