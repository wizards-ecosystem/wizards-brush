import { describe, expect, it } from "vitest";
import type { Control } from "../api/types";
import { coerceValues, reuseValues, unrecordedInMetadata } from "./generators";

const finishing: Control = {
  name: "finish_steps",
  label: "Finishing steps",
  type: "finishing",
  default: [],
  processors: [
    {
      name: "background_removal",
      label: "Remove background",
      description: "",
      available: true,
      unavailable_reason: null,
      options: [],
    },
    {
      name: "resize",
      label: "Resize",
      description: "",
      available: true,
      unavailable_reason: null,
      options: [],
    },
  ],
};

const chain = [
  { processor: "background_removal" },
  { processor: "resize", width: 640, height: 360, mode: "cover", background: "#000000" },
];

describe("restoring an ordered finishing chain", () => {
  it("keeps order and non-default option values through a JSON draft", () => {
    const draft = JSON.parse(JSON.stringify({ finish_steps: chain }));
    const restored = coerceValues([finishing], draft);
    expect(restored.values.finish_steps).toEqual(chain);
    expect(restored.adjusted).toEqual([]);
  });

  it("does not reorder: the reverse chain is a different request", () => {
    const reversed = [...chain].reverse();
    expect(coerceValues([finishing], { finish_steps: reversed }).values.finish_steps).toEqual(reversed);
  });

  it("drops only an unavailable processor, by name, and keeps the rest", () => {
    const stale = [chain[0], { processor: "sharpen_v1", amount: 3 }, chain[1]];
    const restored = coerceValues([finishing], { finish_steps: stale });
    expect(restored.values.finish_steps).toEqual(chain);
    expect(restored.adjusted).toEqual(["Finishing steps (dropped unavailable: sharpen_v1)"]);
  });

  it("treats a non-list as stale and falls back to the default", () => {
    const restored = coerceValues([finishing], { finish_steps: "background_removal" });
    expect(restored.values.finish_steps).toEqual([]);
    expect(restored.adjusted).toEqual(["Finishing steps"]);
  });
});

describe("reuse from image metadata", () => {
  it("lists what a file's metadata cannot restore", () => {
    const meta = { prompt: "a fox", seed: 3 };
    expect(unrecordedInMetadata(meta, "local_image:txt2img")).toEqual([
      "adapters",
      "finishing steps",
      "negative prompt",
    ]);
    expect(unrecordedInMetadata(meta, "colab_inpaint")).toEqual(
      expect.arrayContaining(["source images", "mask"]),
    );
    expect(reuseValues(meta).prompt).toBe("a fox");
  });
});
