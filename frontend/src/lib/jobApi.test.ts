import { describe, expect, it } from "vitest";
import type { GeneratorSpec } from "../api/types";
import { apiRequestText, jobRequestFor, mcpCallText, openaiCallText } from "./jobApi";

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

describe("mcpCallText", () => {
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

  it("snapshots an edit call and drops the wait note", () => {
    const { text, notes } = mcpCallText(spec, { guidance: 2 }, "raw, in watercolour");
    expect(notes).toEqual([
      "Put input asset ids in inputs.images and the mask's id in inputs.mask; import local files with POST /api/assets/import (role=mask for the mask).",
    ]);
    expect(text).toMatchInlineSnapshot(`
      "{
        "tool": "edit",
        "arguments": {
          "instruction": "raw, in watercolour",
          "images": [
            "<asset id>"
          ],
          "mask": "<mask asset id>",
          "params": {
            "guidance": 2
          },
          "wait": true
        }
      }"
    `);
  });
});

describe("openaiCallText", () => {
  const local = {
    id: "image_local",
    kind: "image_local",
    title: "Local",
    subtitle: "",
    endpoint: "/api/generate/image-local",
    output: "image",
    needs_image: false,
    needs_remote: false,
    controls: [
      { name: "prompt", label: "Prompt", type: "textarea", default: "" },
      { name: "model_variant", label: "Model", type: "select", default: "turbo" },
      { name: "aspect", label: "Aspect", type: "select", default: "1:1" },
      { name: "batch", label: "Batch", type: "number", default: 1 },
      { name: "width", label: "Width", type: "number", default: 1024 },
      { name: "height", label: "Height", type: "number", default: 1024 },
    ],
  } as GeneratorSpec;

  it("snapshots a generate snippet with an SDK-sized ratio", () => {
    expect(openaiCallText(local, { model_variant: "klein", aspect: "16:9", batch: 2 }, "a ceramic cup"))
      .toMatchInlineSnapshot(`
      "import os

      from openai import OpenAI

      client = OpenAI(
          base_url="http://127.0.0.1:8000/api/openai/v1",
          api_key=os.environ["BRUSH_API_TOKEN"],
      )
      image = client.images.generate(
          model="local/klein",
          prompt="a ceramic cup",
          size="1536x1024",
          n=2,
      )"
    `);
  });

  it("omits size for match-input edits and refuses unmapped kinds", () => {
    const edit = {
      ...local,
      kind: "img2img",
      needs_image: true,
    } as GeneratorSpec;
    const text = openaiCallText(edit, { aspect: "Match input", model_variant: "quality" }, "warmer light");
    expect(text).toContain("client.images.edit(");
    expect(text).toContain('model="local/quality"');
    expect(text).toContain("http URLs are refused");
    expect(text).not.toContain("size=");
    expect(openaiCallText({ ...local, kind: "t2v" }, {}, "a pan")).toBeNull();
  });
});
