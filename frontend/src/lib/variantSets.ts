import type {
  VariantFinishingStep,
  VariantItemState,
  VariantRecipeSpec,
  VariantStageSpec,
  VariantValidationSpec,
} from "../api/types";

/**
 * The Variant Set form's draft model and the pure logic around it.
 *
 * Nothing here decides policy — the backend compiles every recipe and is the
 * authority on what is valid. This file only turns the form into a recipe and
 * back, counts combinations live as values are typed, and points out mistakes
 * early so the preview request is not the first place a typo is seen.
 */

export const AXIS_NAME = /^[A-Za-z][A-Za-z0-9_]{0,31}$/;

export interface AxisDraft {
  uid: string;
  name: string;
  values: string[];
  /** Show the per-value description editor (the recipe's content map). */
  describe: boolean;
  content: Record<string, string>;
}

export interface ValidationDraft {
  png: boolean;
  exactSize: boolean;
  width: number;
  height: number;
  alpha: "any" | "required" | "forbidden";
  corners: boolean;
  /** Percentages in the form; fractions in the recipe. Blank = not checked. */
  minTransparent: number | "";
  maxTransparent: number | "";
  margin: number | "";
}

export interface StageDraft {
  uid: string;
  name: string;
  operation: string;
  axes: AxisDraft[];
  prompt: string;
  negative: string;
  /** Registry control values for the operation. */
  params: Record<string, any>;
  /** axis -> value -> settings that value changes. Edited through the API or a
   *  saved recipe; the form carries them so a round trip never drops one. */
  valueParams: Record<string, Record<string, Record<string, any>>>;
  /** Extra reference images for a later image_edit stage (asset ids). */
  references: number[];
  /** Ordered finishing processors; see FinishingStepsEditor. */
  finishing: VariantFinishingStep[];
  validation: ValidationDraft;
  naming: { template: string; prefix: string };
}

export interface SetDraft {
  name: string;
  sources: number[];
  maskAssetId: number | null;
  seedMode: "fixed" | "per_variant";
  seed: number;
  collection: boolean;
  stages: StageDraft[];
}

let uidCounter = 0;
export const uid = (prefix = "d") => `${prefix}${Date.now().toString(36)}${(uidCounter++).toString(36)}`;

export function newAxis(name = ""): AxisDraft {
  return { uid: uid("a"), name, values: [], describe: false, content: {} };
}

export function newStage(operation: string, name = ""): StageDraft {
  return {
    uid: uid("s"),
    name,
    operation,
    axes: [newAxis()],
    prompt: "",
    negative: "",
    params: {},
    valueParams: {},
    references: [],
    finishing: [],
    validation: {
      png: false,
      exactSize: false,
      width: 1024,
      height: 1024,
      alpha: "any",
      corners: false,
      minTransparent: "",
      maxTransparent: "",
      margin: "",
    },
    naming: { template: "", prefix: "" },
  };
}

export function newDraft(operation: string): SetDraft {
  return {
    name: "",
    sources: [],
    maskAssetId: null,
    seedMode: "fixed",
    seed: -1,
    collection: true,
    stages: [newStage(operation)],
  };
}

// ---- counting -------------------------------------------------------------------
export interface Combinations {
  /** Combinations each stage adds per item of the stage before it. */
  perStage: number[];
  /** Items at each stage: the product of every stage so far. */
  totals: number[];
  /** Every generation the set will run. */
  total: number;
}

/** The same arithmetic the backend uses before it builds anything: a stage's
 *  count is the product of its axes' value counts (no axes = one), and every
 *  stage runs once per item of the stage before it. */
export function countCombinations(stages: { axes: { values: unknown[] }[] }[]): Combinations {
  const perStage = stages.map((stage) => stage.axes.reduce((n, axis) => n * axis.values.length, 1));
  const totals: number[] = [];
  let running = 1;
  for (const n of perStage) {
    running *= n;
    totals.push(running);
  }
  return { perStage, totals, total: totals.reduce((sum, n) => sum + n, 0) };
}

/** "3 × 2 × 4 × 2" for one stage's axes. */
export function formula(axes: { values: unknown[] }[]): string {
  return axes.length ? axes.map((axis) => axis.values.length).join(" × ") : "1";
}

// ---- values and identifiers --------------------------------------------------------
/** Values from pasted text: split on commas and new lines, trimmed, blanks dropped. */
export function parseAxisValues(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((value) => value.trim())
    .filter(Boolean);
}

/** A value's identifier, mirroring the backend's `expansion.slug`: case- and
 *  width-folded, every run of non letters/digits one hyphen. Two values with one
 *  identifier are ambiguous and the backend refuses them. */
export function slug(value: string): string {
  return value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/, "");
}

/** Add values to an axis, skipping any whose identifier it already has. */
export function addValues(existing: string[], incoming: string[]): string[] {
  const seen = new Set(existing.map(slug));
  const out = [...existing];
  for (const value of incoming) {
    const id = slug(value);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(value);
  }
  return out;
}

/** Early, human-readable problems with the axes of every stage. */
export function axisProblems(stages: Pick<StageDraft, "axes">[]): string[] {
  const problems: string[] = [];
  const names = new Map<string, string>();
  stages.forEach((stage, index) => {
    const where = stages.length > 1 ? `Stage ${index + 1}: ` : "";
    stage.axes.forEach((axis, position) => {
      // An unnamed axis is referred to by its place, never as `axis ""`.
      const label = axis.name ? `Axis "${axis.name}"` : `Axis ${position + 1}`;
      if (!axis.name) {
        problems.push(`${where}${label} needs a name.`);
      } else if (!AXIS_NAME.test(axis.name)) {
        problems.push(`${where}${label} must start with a letter and use only letters, digits and _.`);
      } else {
        const folded = axis.name.toLowerCase();
        if (names.has(folded)) problems.push(`Axis name "${axis.name}" is used twice.`);
        names.set(folded, axis.name);
      }
      if (!axis.values.length) problems.push(`${where}${label} needs at least one value.`);
      const ids = new Map<string, string>();
      for (const value of axis.values) {
        if (value.includes("{{") || value.includes("}}")) {
          problems.push(`${where}value "${value}" contains {{ or }}.`);
        }
        const id = slug(value);
        if (!id) problems.push(`${where}value "${value}" has no letters or digits.`);
        else if (ids.has(id)) problems.push(`${where}"${ids.get(id)}" and "${value}" are the same value.`);
        ids.set(id, value);
      }
    });
  });
  return [...new Set(problems)];
}

// ---- templates -----------------------------------------------------------------------
export const placeholder = (name: string) => `{{${name}}}`;

/** Insert a placeholder at the cursor; returns the new text and cursor. */
export function insertAt(text: string, start: number, end: number, snippet: string) {
  const before = text.slice(0, start);
  const after = text.slice(end);
  return { text: `${before}${snippet}${after}`, cursor: before.length + snippet.length };
}

/** Axes a stage's templates can use: every earlier stage's, then its own. */
export function availableAxes(stages: Pick<StageDraft, "axes">[], index: number): string[] {
  return stages
    .slice(0, index + 1)
    .flatMap((stage) => stage.axes.map((axis) => axis.name))
    .filter(Boolean);
}

// ---- draft <-> recipe ---------------------------------------------------------------------
/** A stage's finishing as saved by an earlier version of this form
 *  (`variant-set-draft-v1` before steps became an ordered list). */
interface LegacyFinishing {
  removeBackground?: boolean;
  resize?: boolean;
  width?: number;
  height?: number;
  mode?: string;
  background?: string;
  extra?: VariantFinishingStep[];
}

function legacySteps(old: LegacyFinishing): VariantFinishingStep[] {
  const steps: VariantFinishingStep[] = [];
  if (old.removeBackground) steps.push({ processor: "background_removal" });
  if (old.resize) {
    steps.push({
      processor: "resize",
      width: old.width ?? 1024,
      height: old.height ?? 1024,
      mode: old.mode ?? "contain",
      background: old.background ?? "transparent",
    });
  }
  return [...steps, ...(old.extra ?? [])];
}

/** A draft read back from storage, upgraded to the current shape. Anything
 *  unreadable is dropped rather than crashing the form. */
export function normalizeDraft(raw: unknown): SetDraft | null {
  if (!raw || typeof raw !== "object" || !Array.isArray((raw as SetDraft).stages)) return null;
  const draft = raw as SetDraft;
  return {
    ...draft,
    stages: draft.stages.map((stage) => {
      const fresh = newStage(stage.operation, stage.name);
      const finishing = Array.isArray(stage.finishing)
        ? stage.finishing
        : legacySteps((stage.finishing ?? {}) as LegacyFinishing);
      return {
        ...fresh,
        ...stage,
        finishing,
        valueParams: stage.valueParams ?? {},
        references: stage.references ?? [],
      };
    }),
  };
}

function validationSpec(validation: ValidationDraft): VariantValidationSpec {
  const spec: VariantValidationSpec = { alpha: validation.alpha };
  if (validation.png) spec.format = "PNG";
  if (validation.exactSize) {
    spec.width = validation.width;
    spec.height = validation.height;
  }
  if (validation.corners) spec.corners_transparent = true;
  if (validation.minTransparent !== "") spec.min_transparent_fraction = validation.minTransparent / 100;
  if (validation.maxTransparent !== "") spec.max_transparent_fraction = validation.maxTransparent / 100;
  if (validation.margin !== "") spec.safe_margin = validation.margin;
  return spec;
}

export const MASK_NAME = "region";

/** The recipe the backend compiles. `maskOps` are operations that need a mask. */
export function toRecipe(draft: SetDraft, maskOps: Set<string>): VariantRecipeSpec {
  const content: Record<string, Record<string, string>> = {};
  for (const stage of draft.stages) {
    for (const axis of stage.axes) {
      if (!axis.describe) continue;
      const described = Object.entries(axis.content).filter(
        ([value, text]) => axis.values.includes(value) && text.trim(),
      );
      if (described.length) content[axis.name] = Object.fromEntries(described);
    }
  }
  const needsMask = draft.stages.some((stage) => maskOps.has(stage.operation));
  const stages: VariantStageSpec[] = draft.stages.map((stage) => ({
    name: stage.name,
    operation: stage.operation,
    axes: stage.axes.map((axis) => ({ name: axis.name, values: axis.values })),
    prompt: stage.prompt,
    negative_prompt: stage.negative,
    params: stage.params,
    value_params: stage.valueParams,
    references: stage.references,
    mask: maskOps.has(stage.operation) ? MASK_NAME : null,
    finishing: stage.finishing,
    validation: validationSpec(stage.validation),
    naming: { template: stage.naming.template, prefix: stage.naming.prefix },
  }));
  return {
    sources: draft.sources,
    masks: needsMask && draft.maskAssetId != null ? { [MASK_NAME]: { asset_id: draft.maskAssetId } } : {},
    content,
    seed: { mode: draft.seedMode, value: draft.seed },
    stages,
  };
}

/** A saved recipe back into the form. Everything the form cannot edit directly
 *  (per-value settings, references) is carried through untouched, so loading
 *  and saving never silently drops a part of the recipe. */
export function fromRecipe(recipe: VariantRecipeSpec, name = ""): SetDraft {
  const content = recipe.content || {};
  const stages = recipe.stages.map((spec) => {
    const stage = newStage(spec.operation, spec.name || "");
    stage.axes = spec.axes.map((axis) => ({
      uid: uid("a"),
      name: axis.name,
      values: axis.values.map(String),
      describe: !!content[axis.name],
      content: { ...(content[axis.name] || {}) },
    }));
    stage.prompt = spec.prompt || "";
    stage.negative = spec.negative_prompt || "";
    stage.params = { ...(spec.params || {}) };
    stage.valueParams = structuredClone(spec.value_params || {});
    stage.references = [...(spec.references || [])];
    stage.finishing = (spec.finishing || []).map((step) => ({ ...step }));
    const v = spec.validation || {};
    stage.validation = {
      png: v.format === "PNG",
      exactSize: v.width != null || v.height != null,
      width: v.width ?? 1024,
      height: v.height ?? 1024,
      alpha: v.alpha || "any",
      corners: !!v.corners_transparent,
      minTransparent: v.min_transparent_fraction != null ? Math.round(v.min_transparent_fraction * 100) : "",
      maxTransparent: v.max_transparent_fraction != null ? Math.round(v.max_transparent_fraction * 100) : "",
      margin: v.safe_margin ?? "",
    };
    stage.naming = { template: spec.naming?.template || "", prefix: spec.naming?.prefix || "" };
    return stage;
  });
  const maskId = Object.values(recipe.masks || {})[0]?.asset_id ?? null;
  return {
    name,
    sources: [...(recipe.sources || [])],
    maskAssetId: maskId,
    seedMode: recipe.seed?.mode || "fixed",
    seed: recipe.seed?.value ?? -1,
    collection: true,
    stages,
  };
}

// ---- presentation ---------------------------------------------------------------------------
export const ITEM_STATE_LABEL: Record<VariantItemState | "running", string> = {
  pending: "Waiting",
  queued: "Queued",
  running: "Running",
  succeeded: "Done",
  invalid: "Invalid",
  failed: "Failed",
  canceled: "Canceled",
  blocked: "Blocked",
};

export function stateTone(state: string): string {
  if (state === "succeeded") return "text-ok";
  if (state === "failed" || state === "invalid") return "text-danger";
  if (state === "blocked" || state === "canceled") return "text-warn";
  if (state === "running" || state === "queued") return "text-accent2";
  return "text-muted";
}

/** An item's displayed state: `queued` splits into queued/running by its job. */
export function liveState(state: VariantItemState, jobStatus: string | null | undefined) {
  return state === "queued" && jobStatus === "running" ? "running" : state;
}

/** How much of a set is settled, for a progress bar. */
export function settledFraction(counts: {
  total?: number;
  succeeded?: number;
  failed?: number;
  invalid?: number;
  canceled?: number;
  blocked?: number;
}): number {
  const total = counts.total || 0;
  if (!total) return 0;
  const settled =
    (counts.succeeded || 0) +
    (counts.failed || 0) +
    (counts.invalid || 0) +
    (counts.canceled || 0) +
    (counts.blocked || 0);
  return Math.min(1, settled / total);
}

type NoticeCounts = Parameters<typeof settledFraction>[0];

/** The one announcement a set earns when it settles, instead of one toast per
 *  failed child. Only a transition seen live counts: a set that was already
 *  finished when the page loaded is not news, and a cancel was the user's own. */
export function settledNotice(
  previous: string | undefined,
  event: { name?: string; status?: string; counts?: NoticeCounts },
): { text: string; kind: "success" | "error" } | null {
  if (previous !== "active") return null;
  if (event.status !== "complete" && event.status !== "incomplete") return null;
  const c = event.counts ?? {};
  const name = event.name ? `“${event.name}”` : "Variant set";
  const done = c.succeeded || 0;
  if (event.status === "complete") {
    return { text: `${name} finished: ${done} variant${done === 1 ? "" : "s"} ready`, kind: "success" };
  }
  const parts = [`${done} done`];
  for (const key of ["failed", "invalid", "blocked", "canceled"] as const) {
    if (c[key]) parts.push(`${c[key]} ${key}`);
  }
  return { text: `${name} finished with problems: ${parts.join(", ")}`, kind: "error" };
}

/** A job that belongs to a Variant Set reports through its set, not on its own. */
export function isVariantGroup(groupId: string | null | undefined): boolean {
  return !!groupId && groupId.startsWith("vset-");
}
