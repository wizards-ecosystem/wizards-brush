import { describe, expect, it } from "vitest";
import type { PresetRead } from "../api/types";
import {
  DRAFT_KEY,
  RECOVERED_KEY,
  customizations,
  emptyDraft,
  loadDraft,
  resetToPreset,
  saveDraft,
  switchPreset,
  toRequest,
} from "./studioDraft";

const preset = (id: string, roles: string[] = []): PresetRead =>
  ({
    id,
    revision: 2,
    head_revision: 2,
    definition: {
      name: id,
      operation: "edit",
      model: { resource: "hf:a@b" },
      references: roles.map((role) => ({ role })),
    },
  }) as unknown as PresetRead;

function memory(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
    clear: () => data.clear(),
    key: () => null,
    length: 0,
  } as Storage;
}

describe("the studio draft", () => {
  it("sends only what the person chose; the preset supplies the rest", () => {
    const draft = { ...emptyDraft(), presetId: "custom/a", revision: 3, prompt: "a fox" };
    expect(toRequest(draft)).toEqual({ preset_id: "custom/a", revision: 3, prompt: "a fox", target: "auto" });
    const chosen = { ...draft, settings: { guidance: 2 }, adapters: [], images: [4], allowRemote: true };
    expect(toRequest(chosen)).toMatchObject({
      settings: { guidance: 2 },
      adapters: [],
      inputs: { images: [4] },
      allow_remote: true,
    });
  });

  it("knows exactly what is customized, and reset restores the preset", () => {
    const draft = {
      ...emptyDraft(),
      presetId: "p",
      prompt: "x",
      settings: { aspect: "16:9" },
      model: "hf:m@1",
    };
    expect(customizations(draft)).toEqual(["model", "aspect"]);
    const reset = resetToPreset(draft);
    expect(customizations(reset)).toEqual([]);
    expect(reset.prompt).toBe("x");
  });

  it("switching presets keeps the subject and inputs and says what did not carry", () => {
    const draft = {
      ...emptyDraft(),
      presetId: "old",
      prompt: "a lighthouse",
      images: [7],
      references: { character: [1], product: [2] },
      settings: { guidance: 3 },
    };
    const { draft: next, dropped } = switchPreset(draft, preset("new", ["character"]));
    expect(next.prompt).toBe("a lighthouse");
    expect(next.images).toEqual([7]);
    expect(next.references).toEqual({ character: [1] });
    expect(next.settings).toEqual({});
    expect(next.revision).toBe(2);
    expect(dropped).toEqual(["guidance", "product reference"]);
  });

  it("round-trips through storage and keeps what it cannot read", () => {
    const storage = memory();
    const draft = {
      ...emptyDraft(),
      presetId: "p",
      prompt: "keep",
      finish: [{ processor: "resize", width: 10 }],
    };
    saveDraft(storage, draft);
    expect(loadDraft(storage).draft).toEqual(draft);
    storage.setItem(DRAFT_KEY, JSON.stringify({ ...draft, legacy_field: 1 }));
    const migrated = loadDraft(storage);
    expect(migrated.draft.prompt).toBe("keep");
    expect(migrated.warning).toMatch(/legacy_field/);
    expect(storage.getItem(RECOVERED_KEY)).toContain("legacy_field");
    storage.setItem(DRAFT_KEY, "{not json");
    expect(loadDraft(storage).warning).toMatch(/kept for recovery/);
    expect(storage.getItem(RECOVERED_KEY)).toBe("{not json");
  });

  it("sends a reference set per role and keeps it across presets that share the role", () => {
    const draft = { ...emptyDraft(), presetId: "p", referenceSets: { character: 4, product: 9 } };
    expect(toRequest(draft)?.inputs).toEqual({
      reference_sets: { character: { id: 4 }, product: { id: 9 } },
    });
    const { draft: next, dropped } = switchPreset(draft, preset("new", ["character"]));
    expect(next.referenceSets).toEqual({ character: 4 });
    expect(dropped).toEqual(["product reference"]);
  });

  it("an older stored draft without reference sets still loads", () => {
    const storage = memory();
    const old = { ...emptyDraft(), presetId: "p" } as Record<string, unknown>;
    delete old.referenceSets;
    storage.setItem(DRAFT_KEY, JSON.stringify(old));
    const { draft, warning } = loadDraft(storage);
    expect(draft.referenceSets).toEqual({});
    expect(warning).toBe("");
  });
});
