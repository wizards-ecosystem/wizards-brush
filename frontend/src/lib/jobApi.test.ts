import { describe, expect, it } from "vitest";
import type { GeneratorSpec } from "../api/types";
import { apiRequestText, jobRequestFor } from "./jobApi";

describe("apiRequestText", () => {
  it("writes a request as a runnable curl command, quotes and all", () => {
    const text = apiRequestText("/api/variant-sets", { name: "it's here" }, "http://127.0.0.1:8000");
    expect(text.split("\n")[0]).toBe("curl -X POST http://127.0.0.1:8000/api/variant-sets \\");
    expect(text).toContain('-H "X-API-Token: ${API_TOKEN:-}"');
    // A single quote in the JSON closes, escapes and reopens the shell string.
    expect(text).toContain("it'\\''s here");
  });

  it("puts notes first, as shell comments", () => {
    const text = apiRequestText("/api/jobs", {}, "http://x", ["import inputs first"]);
    expect(text.split("\n").slice(0, 2)).toEqual([
      "# import inputs first",
      "curl -X POST http://x/api/jobs \\",
    ]);
  });
});

describe("jobRequestFor", () => {
  const spec = {
    id: "inpaint",
    kind: "inpaint",
    title: "Inpaint",
    subtitle: "",
    endpoint: "/api/generate/image/inpaint",
    output: "image",
    needs_image: true,
    needs_mask: true,
    needs_remote: false,
    controls: [
      { name: "prompt", label: "Prompt", type: "textarea", default: "" },
      { name: "guidance", label: "Guidance", type: "slider", default: 1 },
    ],
  } as GeneratorSpec;

  it("sends only the kind's settings, with the composed prompt", () => {
    const { body, notes } = jobRequestFor(
      spec,
      { prompt: "raw", guidance: 2, style_ids: ["s1"], raw_prompt: "raw", request_id: "x" },
      "raw, in watercolour",
    );
    expect(body).toEqual({
      kind: "inpaint",
      params: { prompt: "raw, in watercolour", guidance: 2 },
      inputs: { images: [], mask: null },
    });
    expect(notes[0]).toMatch(/inputs\.images.*inputs\.mask.*POST \/api\/assets\/import/);
  });

  it("needs no inputs note for a text-only generator", () => {
    const { body, notes } = jobRequestFor({ ...spec, needs_image: false, needs_mask: false }, {}, "p");
    expect(body.inputs).toBeUndefined();
    expect(notes).toHaveLength(1);
  });
});
