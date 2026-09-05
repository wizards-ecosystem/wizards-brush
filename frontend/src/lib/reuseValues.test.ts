import { describe, expect, it } from "vitest";
import { MAX_REUSE_SIDE, reuseValues } from "./generators";

describe("reuseValues", () => {
  it("reuses real generation dimensions", () => {
    const v = reuseValues({ prompt: "x", width: 1024, height: 1024, seed: 7 });
    expect(v.aspect).toBe("Custom");
    expect(v.width).toBe(1024);
    expect(v.height).toBe(1024);
  });

  it("ignores post-processed dimensions and falls back to the aspect", () => {
    /* Real case from the gallery: a x4 upscale of 672x896 was stored as
       2688x3584, so "reuse all" asked the model for 16x the area. */
    const v = reuseValues({ prompt: "x", width: 2688, height: 3584, aspect: "3:4", seed: 7 });
    expect(v.aspect).toBe("3:4");
    expect(v.width).toBeUndefined();
    expect(v.height).toBeUndefined();
  });

  it("leaves aspect alone when there is nothing sane to fall back to", () => {
    const v = reuseValues({ prompt: "x", width: 4096, height: 4096, seed: 7 });
    expect(v.width).toBeUndefined();
    expect(v.aspect).toBeUndefined();
  });

  it("accepts dimensions right at the cap", () => {
    const v = reuseValues({ width: MAX_REUSE_SIDE, height: MAX_REUSE_SIDE });
    expect(v.aspect).toBe("Custom");
  });

  it("uses explicit ancestor dimensions for a derivative", () => {
    const v = reuseValues({
      width: 4096,
      height: 4096,
      generation_width: 1024,
      generation_height: 1024,
      post: [{ operation: "upscale", scale: 4 }],
    });
    expect(v.aspect).toBe("Custom");
    expect(v.width).toBe(1024);
    expect(v.height).toBe(1024);
  });

  it("still carries prompt, seed and the rest", () => {
    const v = reuseValues({
      prompt: "a cat, cinematic",
      raw_prompt: "a cat",
      style_ids: ["cinematic"],
      seed: 42,
      steps: 20,
      guidance: 4,
      model_variant: "quality",
      sampler: "euler",
      finish: "faces",
      loras: [],
    });
    expect(v.prompt).toBe("a cat");
    expect(v.seed).toBe(42);
    expect(v.quality).toBe("Custom");
    expect(v.loras).toEqual([]);
    expect(v.style_ids).toEqual(["cinematic"]);
    expect(v.model_variant).toBe("quality");
    expect(v.sampler).toBe("euler");
    expect(v.finish).toBe("faces");
  });
});
