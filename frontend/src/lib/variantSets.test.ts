import { describe, expect, it } from "vitest";
import {
  addValues,
  apiRequestText,
  availableAxes,
  axisProblems,
  countCombinations,
  formula,
  fromRecipe,
  insertAt,
  liveState,
  newDraft,
  newStage,
  normalizeDraft,
  parseAxisValues,
  settledFraction,
  slug,
  toRecipe,
} from "./variantSets";

const axes = (...counts: number[]) =>
  counts.map((n, i) => ({ name: `a${i}`, values: Array.from({ length: n }, (_, j) => `v${j}`) }));

describe("combination counting", () => {
  it("multiplies a stage's axes: 3 × 2 × 4 × 2 = 48", () => {
    const counts = countCombinations([{ axes: axes(3, 2, 4, 2) }]);
    expect(counts).toEqual({ perStage: [48], totals: [48], total: 48 });
    expect(formula(axes(3, 2, 4, 2))).toBe("3 × 2 × 4 × 2");
  });

  it("runs every later stage once per item of the stage before it", () => {
    // 2 bases, then 3 views of each: 2 + 6 generations.
    const counts = countCombinations([{ axes: axes(2) }, { axes: axes(3) }]);
    expect(counts.totals).toEqual([2, 6]);
    expect(counts.total).toBe(8);
  });

  it("treats a stage without axes as one run, and an empty axis as nothing", () => {
    expect(countCombinations([{ axes: [] }]).total).toBe(1);
    expect(countCombinations([{ axes: axes(3, 0) }]).total).toBe(0);
    expect(formula([])).toBe("1");
  });
});

describe("axis values", () => {
  it("splits pasted text on commas and new lines", () => {
    expect(parseAxisValues("wood, steel\nglass,, ")).toEqual(["wood", "steel", "glass"]);
  });

  it("mirrors the backend identifier and skips values that would collide", () => {
    expect(slug("Dark  Grey!")).toBe("dark-grey");
    expect(slug("snake_case")).toBe("snake-case");
    expect(addValues(["Red"], ["red", "dark grey", "Dark-Grey", "blue"])).toEqual([
      "Red",
      "dark grey",
      "blue",
    ]);
  });

  it("reports problems before the server has to", () => {
    const stage = newStage("image_edit");
    stage.axes = [
      { uid: "1", name: "1bad", values: ["x"], describe: false, content: {} },
      { uid: "2", name: "Color", values: [], describe: false, content: {} },
      { uid: "3", name: "color", values: ["x", "{{y}}"], describe: false, content: {} },
    ];
    const problems = axisProblems([stage]).join(" | ");
    expect(problems).toContain('Axis "1bad" must start with a letter');
    expect(problems).toContain('Axis name "color" is used twice');
    expect(problems).toContain('Axis "Color" needs at least one value');
    // A blank axis is named by its place, not as an empty string.
    expect(axisProblems([newStage("image_edit")])).toEqual([
      "Axis 1 needs a name.",
      "Axis 1 needs at least one value.",
    ]);
    expect(problems).toContain("contains {{ or }}");
  });
});

describe("templates", () => {
  it("inserts a placeholder at the cursor", () => {
    expect(insertAt("In  light", 3, 3, "{{material}}")).toEqual({
      text: "In {{material}} light",
      cursor: 15,
    });
  });

  it("offers earlier stages' axes to later stages", () => {
    const first = newStage("image_edit");
    first.axes[0].name = "material";
    const second = newStage("image_edit");
    second.axes[0].name = "angle";
    expect(availableAxes([first, second], 0)).toEqual(["material"]);
    expect(availableAxes([first, second], 1)).toEqual(["material", "angle"]);
  });
});

describe("draft ⇄ recipe", () => {
  it("builds the recipe the backend compiles and reads it back", () => {
    const draft = newDraft("image_edit");
    draft.sources = [12];
    draft.seedMode = "per_variant";
    draft.seed = 99;
    const stage = draft.stages[0];
    stage.axes = [
      {
        uid: "a",
        name: "lighting",
        values: ["soft", "dramatic"],
        describe: true,
        content: { soft: "soft even light", dramatic: "", stale: "dropped" },
      },
    ];
    stage.prompt = "Lit by {{lighting}}";
    stage.params = { quality: "High" };
    stage.finishing = [
      { processor: "background_removal" },
      { processor: "resize", width: 512, height: 512, mode: "contain", background: "transparent" },
      { processor: "upscale", scale: 2 },
    ];
    stage.valueParams = { lighting: { dramatic: { guidance: 3 } } };
    stage.references = [31];
    stage.validation.png = true;
    stage.validation.alpha = "required";
    stage.validation.corners = true;
    stage.validation.minTransparent = 20;
    stage.naming.template = "{{lighting}}";

    const recipe = toRecipe(draft, new Set());
    expect(recipe.sources).toEqual([12]);
    expect(recipe.seed).toEqual({ mode: "per_variant", value: 99 });
    // Only described, still-present values reach the content map.
    expect(recipe.content).toEqual({ lighting: { soft: "soft even light" } });
    expect(recipe.stages[0].finishing).toEqual([
      { processor: "background_removal" },
      { processor: "resize", width: 512, height: 512, mode: "contain", background: "transparent" },
      { processor: "upscale", scale: 2 },
    ]);
    expect(recipe.stages[0].validation).toEqual({
      alpha: "required",
      format: "PNG",
      corners_transparent: true,
      min_transparent_fraction: 0.2,
    });
    expect(recipe.stages[0].mask).toBeNull();
    expect(recipe.stages[0].value_params).toEqual({ lighting: { dramatic: { guidance: 3 } } });
    expect(recipe.stages[0].references).toEqual([31]);

    const back = fromRecipe(recipe, "Lighting");
    expect(back.name).toBe("Lighting");
    expect(back.stages[0].axes[0].values).toEqual(["soft", "dramatic"]);
    // Order is kept: resize-then-upscale is not the same image as the reverse.
    expect(back.stages[0].finishing.map((s) => s.processor)).toEqual([
      "background_removal",
      "resize",
      "upscale",
    ]);
    expect(back.stages[0].validation.minTransparent).toBe(20);
    expect(toRecipe(back, new Set()).stages[0]).toEqual(recipe.stages[0]);
  });

  it("upgrades a draft saved before finishing became an ordered list", () => {
    const old = newDraft("image_edit") as any;
    old.stages[0].finishing = {
      removeBackground: true,
      resize: true,
      width: 640,
      height: 480,
      mode: "cover",
      background: "#ffffff",
      extra: [{ processor: "face_restore" }],
    };
    delete old.stages[0].valueParams;
    delete old.stages[0].references;
    const draft = normalizeDraft(JSON.parse(JSON.stringify(old)))!;
    expect(draft.stages[0].finishing).toEqual([
      { processor: "background_removal" },
      { processor: "resize", width: 640, height: 480, mode: "cover", background: "#ffffff" },
      { processor: "face_restore" },
    ]);
    expect(draft.stages[0].valueParams).toEqual({});
    expect(draft.stages[0].references).toEqual([]);
    expect(normalizeDraft({ nonsense: true })).toBeNull();
    expect(normalizeDraft(null)).toBeNull();
  });

  it("names the mask only for operations that need one", () => {
    const draft = newDraft("inpaint");
    draft.maskAssetId = 7;
    const recipe = toRecipe(draft, new Set(["inpaint"]));
    expect(recipe.masks).toEqual({ region: { asset_id: 7 } });
    expect(recipe.stages[0].mask).toBe("region");
  });
});

describe("presentation", () => {
  it("splits queued into queued and running from the job", () => {
    expect(liveState("queued", "running")).toBe("running");
    expect(liveState("queued", "queued")).toBe("queued");
    expect(liveState("failed", "error")).toBe("failed");
  });

  it("measures how much of a set has settled", () => {
    expect(settledFraction({ total: 4, succeeded: 1, failed: 1, blocked: 1 })).toBe(0.75);
    expect(settledFraction({})).toBe(0);
  });

  it("writes a request as a runnable curl command, quotes and all", () => {
    const text = apiRequestText("/api/variant-sets", { name: "it's here" }, "http://127.0.0.1:8000");
    expect(text.split("\n")[0]).toBe("curl -X POST http://127.0.0.1:8000/api/variant-sets \\");
    expect(text).toContain('-H "X-API-Token: ${API_TOKEN:-}"');
    // A single quote in the JSON closes, escapes and reopens the shell string.
    expect(text).toContain("it'\\''s here");
  });
});
