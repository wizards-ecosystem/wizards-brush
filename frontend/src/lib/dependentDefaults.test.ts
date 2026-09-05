import { describe, expect, it } from "vitest";
import { applyDependentDefaults, coerceValues } from "./generators";
import type { Control } from "../api/types";

const guidance: Control = {
  name: "guidance",
  label: "Guidance",
  type: "slider",
  default: 1.0,
  defaults_by: { field: "model_variant", map: { turbo: 1.0, quality: 4.0 } },
};
const unrelated: Control = { name: "steps", label: "Steps", type: "slider", default: 9 };

describe("applyDependentDefaults", () => {
  it("moves guidance to the CFG default when switching turbo → quality", () => {
    // The actual bug: picking Quality kept CFG at 1.0 and quietly ran a
    // real-CFG model in a regime it was never trained for.
    const out = applyDependentDefaults([guidance, unrelated], "model_variant", "turbo", "quality", {
      guidance: 1.0,
    });
    expect(out).toEqual({ guidance: 4.0 });
  });

  it("moves it back when switching quality → turbo", () => {
    const out = applyDependentDefaults([guidance], "model_variant", "quality", "turbo", {
      guidance: 4.0,
    });
    expect(out).toEqual({ guidance: 1.0 });
  });

  it("never overrides a value the user set by hand", () => {
    const out = applyDependentDefaults([guidance], "model_variant", "turbo", "quality", {
      guidance: 2.5,
    });
    expect(out).toEqual({});
  });

  it("fills in when the control has no value yet", () => {
    const out = applyDependentDefaults([guidance], "model_variant", "turbo", "quality", {});
    expect(out).toEqual({ guidance: 4.0 });
  });

  it("ignores controls that do not watch the changed field", () => {
    const out = applyDependentDefaults([guidance], "quality", "Draft", "High", { guidance: 1.0 });
    expect(out).toEqual({});
  });

  it("ignores a value the map has no entry for", () => {
    const out = applyDependentDefaults([guidance], "model_variant", "turbo", "mystery", {
      guidance: 1.0,
    });
    expect(out).toEqual({});
  });

  it("leaves controls without defaults_by alone", () => {
    const out = applyDependentDefaults([unrelated], "model_variant", "turbo", "quality", {
      steps: 9,
    });
    expect(out).toEqual({});
  });

  it("reconciles a value only when a newly selected range cannot represent it", () => {
    const frames: Control = {
      name: "num_frames",
      label: "Length",
      type: "slider",
      default: 49,
      min: 25,
      max: 121,
      step: 4,
      overrides_by: {
        field: "engine",
        map: {
          wan: { label: "Length (frames · 4n+1)", min: 25, max: 121, step: 4 },
          ltx: { label: "Length (frames)", min: 9, max: 129, step: 1 },
        },
      },
    };
    expect(applyDependentDefaults([frames], "engine", "ltx", "wan", { num_frames: 30 })).toEqual({
      num_frames: 29,
    });
    expect(applyDependentDefaults([frames], "engine", "wan", "ltx", { num_frames: 49 })).toEqual({});
  });
});

describe("coerceValues", () => {
  const controls = [
    {
      name: "model_variant",
      label: "Model",
      type: "select" as const,
      default: "turbo",
      options: ["turbo", "quality"],
    },
    { ...guidance, min: 0, max: 8 },
    { name: "batch", label: "Batch", type: "slider" as const, default: 1, min: 1, max: 8 },
  ];

  it("drops unknown fields and snaps invalid options/ranges", () => {
    const out = coerceValues(controls, { model_variant: "removed", guidance: 6, batch: 99, ghost: true });
    expect(out.values.model_variant).toBe("turbo");
    expect(out.values.guidance).toBe(1);
    expect(out.values.batch).toBe(8);
    expect(out.values.ghost).toBeUndefined();
    expect(out.adjusted).toEqual(expect.arrayContaining(["Model", "Guidance", "Batch", "ghost"]));
  });

  it("uses the dependent default for a valid restored model when guidance is absent", () => {
    const out = coerceValues(controls, { model_variant: "quality" });
    expect(out.values.guidance).toBe(4);
  });

  it("filters stale style chips against the live catalogue", () => {
    const out = coerceValues(controls, { style_ids: ["photo", "gone"] }, new Set(["photo"]));
    expect(out.values.style_ids).toEqual(["photo"]);
    expect(out.adjusted).toContain("Styles");
  });
});
