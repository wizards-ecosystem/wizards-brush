import type { PlanRequest, PresetRead, Target } from "../api/types";

/**
 * The Studio's draft: a preset revision plus only what the person chose.
 *
 * Everything the preset supplies stays on the server. The draft holds the
 * subject prompt, inputs by asset id, and explicit overrides; `null` means
 * "the preset's value". That is what keeps a preset's defaults, dependent
 * defaults and prompt composition in one place (the resolver) and what makes
 * "customized" a fact rather than a guess.
 */
export interface StudioDraft {
  v: 1;
  presetId: string | null;
  /** The revision this draft was started from. Reset restores exactly it;
   *  upgrading to a newer one is a separate, explicit step. */
  revision: number | null;
  prompt: string;
  negative: string | null;
  styles: string[] | null;
  enhance: boolean | null;
  notes: string;
  settings: Record<string, unknown>;
  model: string | null;
  adapters: { resource: string; strength: number }[] | null;
  finish: Record<string, unknown>[] | null;
  images: number[];
  mask: number | null;
  references: Record<string, number[]>;
  target: Target;
  allowRemote: boolean;
  seed: number | null;
}

export const DRAFT_KEY = "studio-draft-v1";
export const RECOVERED_KEY = "studio-draft-v1-recovered";

export function emptyDraft(): StudioDraft {
  return {
    v: 1,
    presetId: null,
    revision: null,
    prompt: "",
    negative: null,
    styles: null,
    enhance: null,
    notes: "",
    settings: {},
    model: null,
    adapters: null,
    finish: null,
    images: [],
    mask: null,
    references: {},
    target: "auto",
    allowRemote: false,
    seed: null,
  };
}

/** The preset request the draft stands for, or null with no preset. */
export function toRequest(draft: StudioDraft): PlanRequest | null {
  if (!draft.presetId) return null;
  const request: PlanRequest = { preset_id: draft.presetId, prompt: draft.prompt, target: draft.target };
  if (draft.revision != null) request.revision = draft.revision;
  if (draft.negative !== null) request.negative_prompt = draft.negative;
  if (draft.styles !== null) request.styles = draft.styles;
  if (draft.enhance !== null) request.enhance = draft.enhance;
  if (draft.notes) request.notes = draft.notes;
  if (Object.keys(draft.settings).length) request.settings = draft.settings;
  if (draft.model !== null) request.model = draft.model;
  if (draft.adapters !== null) request.adapters = draft.adapters;
  if (draft.finish !== null) request.finish = draft.finish;
  if (draft.allowRemote) request.allow_remote = true;
  if (draft.seed !== null) request.seed = draft.seed;
  const inputs: NonNullable<PlanRequest["inputs"]> = {};
  if (draft.images.length) inputs.images = draft.images;
  if (draft.mask !== null) inputs.mask = draft.mask;
  if (Object.keys(draft.references).length) inputs.references = draft.references;
  if (Object.keys(inputs).length) request.inputs = inputs;
  return request;
}

/** What the person changed from the preset revision, by field name. */
export function customizations(draft: StudioDraft): string[] {
  const changed: string[] = [];
  if (draft.model !== null) changed.push("model");
  if (draft.adapters !== null) changed.push("adapters");
  if (draft.finish !== null) changed.push("finishing");
  if (draft.negative !== null) changed.push("negative prompt");
  if (draft.styles !== null) changed.push("styles");
  if (draft.enhance !== null) changed.push("enhancement");
  changed.push(...Object.keys(draft.settings));
  return changed;
}

/** Back to the exact preset revision: keep the subject and inputs, drop
 *  every override. */
export function resetToPreset(draft: StudioDraft): StudioDraft {
  return {
    ...draft,
    negative: null,
    styles: null,
    enhance: null,
    settings: {},
    model: null,
    adapters: null,
    finish: null,
  };
}

/**
 * Switch to another preset. The subject prompt and the source images stay;
 * references stay for the roles the new preset also declares; overrides made
 * for the previous preset do not follow, because they were choices about it.
 * Returns the new draft and a short summary of what did not carry over.
 */
export function switchPreset(
  draft: StudioDraft,
  preset: PresetRead,
): { draft: StudioDraft; dropped: string[] } {
  const roles = new Set((preset.definition.references || []).map((r) => r.role));
  const references = Object.fromEntries(Object.entries(draft.references).filter(([role]) => roles.has(role)));
  const droppedRoles = Object.keys(draft.references).filter((role) => !roles.has(role));
  const next: StudioDraft = {
    ...resetToPreset(draft),
    presetId: preset.id,
    revision: preset.revision,
    references,
    notes: draft.notes,
    seed: null,
  };
  return {
    draft: next,
    dropped: [...customizations(draft), ...droppedRoles.map((role) => `${role} reference`)],
  };
}

/** Read a stored draft. Anything that cannot be read is kept, not lost: the
 *  raw text goes to a recovery key and the caller is told what happened. */
export function loadDraft(storage: Storage): { draft: StudioDraft; warning: string } {
  let raw: string | null;
  try {
    raw = storage.getItem(DRAFT_KEY);
  } catch {
    return { draft: emptyDraft(), warning: "" };
  }
  if (!raw) return { draft: emptyDraft(), warning: "" };
  try {
    const parsed = JSON.parse(raw) as Partial<StudioDraft> & Record<string, unknown>;
    const base = emptyDraft();
    if (parsed.v !== 1) throw new Error("unknown draft version");
    const known = new Set(Object.keys(base));
    const unknown = Object.keys(parsed).filter((key) => !known.has(key));
    const draft = { ...base } as StudioDraft;
    for (const key of Object.keys(base) as (keyof StudioDraft)[]) {
      if (!(key in parsed)) continue;
      const value = parsed[key];
      const expected = base[key];
      // A nullable field takes any stored value; the rest must keep their type.
      if (expected === null || typeof value === typeof expected) {
        (draft as unknown as Record<string, unknown>)[key] = value;
      }
    }
    if (unknown.length) {
      storage.setItem(RECOVERED_KEY, raw);
      return {
        draft,
        warning: `This draft had fields this version does not use (${unknown.join(", ")}); the original was kept for recovery.`,
      };
    }
    return { draft, warning: "" };
  } catch {
    try {
      storage.setItem(RECOVERED_KEY, raw);
    } catch {
      /* storage full: the warning still says what happened */
    }
    return { draft: emptyDraft(), warning: "A saved draft could not be read; it was kept for recovery." };
  }
}

export function saveDraft(storage: Storage, draft: StudioDraft): void {
  try {
    storage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch {
    /* quota or private mode: the draft stays in memory */
  }
}
