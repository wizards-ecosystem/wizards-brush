import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { VariantCapabilities } from "../api/types";
import { VariantSetCreate } from "./VariantSetCreate";

const mocks = vi.hoisted(() => ({
  variantCapabilities: vi.fn(),
  previewVariantSet: vi.fn(),
  createVariantSet: vi.fn(),
  saveVariantRecipe: vi.fn(),
  importAsset: vi.fn(),
  createJob: vi.fn(),
  waitJob: vi.fn(),
  variantSet: vi.fn(),
  asset: vi.fn(),
  toast: vi.fn(),
  cap: 1000,
}));

vi.mock("../api/client", () => ({
  authHeaders: () => ({}),
  api: {
    variantCapabilities: mocks.variantCapabilities,
    previewVariantSet: mocks.previewVariantSet,
    createVariantSet: mocks.createVariantSet,
    saveVariantRecipe: mocks.saveVariantRecipe,
    variantRecipe: vi.fn(),
    uploadVariantMask: vi.fn(),
    importAsset: mocks.importAsset,
    createJob: mocks.createJob,
    waitJob: mocks.waitJob,
    variantSet: mocks.variantSet,
    asset: mocks.asset,
  },
}));

// The edit operation's controls come from its registry spec — the page must
// render what the registry says and nothing it does not.
const editSpec = {
  id: "image_edit",
  kind: "image_edit",
  title: "Image Edit (A100)",
  subtitle: "",
  endpoint: "/api/generate/image/edit",
  output: "image",
  needs_image: true,
  needs_remote: true,
  controls: [
    { name: "prompt", label: "Prompt", type: "textarea", default: "", tier: "basic" },
    { name: "seed", label: "Seed (-1 random)", type: "number", default: -1 },
    {
      name: "quality",
      label: "Quality",
      type: "segmented",
      default: "Standard",
      options: ["Draft", "Standard", "High"],
      tier: "basic",
    },
    { name: "guidance", label: "Guidance", type: "slider", default: 4, min: 0, max: 12, step: 0.5 },
  ],
};

const source = {
  id: 12,
  kind: "image",
  filename: "source.png",
  url: "/files/images/source.png",
  thumb_url: null,
  width: 64,
  height: 64,
  job_id: null,
  generator: "local_image:txt2img",
  meta: {},
  created_at: "2026-09-18T10:00:00Z",
  favorite: false,
  rating: 0,
  tags: [],
};

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      specs: [editSpec],
      presets: null,
      system: { remote_gpu: { connected: true } },
      assets: [source],
      toast: mocks.toast,
    }),
}));

function caps(cap: number): VariantCapabilities {
  return {
    operations: [
      {
        kind: "image_edit",
        title: "Image Edit (A100)",
        min_sources: 1,
        max_sources: 3,
        mask: "none",
        takes_source: true,
        note: "Edits by instruction.",
        lane: "remote",
      },
    ],
    set_controlled: ["batch", "combinatorial", "negative_prompt", "prompt", "seed", "seed_mode"],
    finishing: [
      {
        name: "background_removal",
        label: "Remove background",
        description: "Real alpha.",
        available: true,
        unavailable_reason: null,
        options: [],
      },
    ],
    validators: ["file", "alpha"],
    cap,
    limits: { stages: 4, axes_per_stage: 12, values_per_axis: 256, value_chars: 120 },
  };
}

function renderCreate(path = "/variants/new") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/variants/new" element={<VariantSetCreate />} />
        <Route path="/variants/:id" element={<div>set page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

async function addAxis(name: string, values: string, index: number) {
  const editors = await screen.findAllByTestId("axis-editor");
  if (index >= editors.length) {
    await userEvent.click(screen.getByRole("button", { name: /add axis/i }));
  }
  const editor = (await screen.findAllByTestId("axis-editor"))[index];
  await userEvent.type(within(editor).getByLabelText("Axis name"), name);
  await userEvent.type(within(editor).getByLabelText(`Add values to ${name}`), `${values},`);
}

describe("Variant Set editor", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.variantCapabilities.mockResolvedValue(caps(1000));
    mocks.previewVariantSet.mockImplementation(async () => ({
      total: 6,
      cap: 1000,
      stages: [],
      items: [
        { stage: 0, key: "k", values: {}, prompt: "p", negative_prompt: "", seed: 1, output_name: "a.png" },
      ],
      collisions: [],
      warnings: [],
    }));
    mocks.createVariantSet.mockResolvedValue({ id: 5, expected: 6 });
  });

  it("counts combinations live as axes and values are added", async () => {
    renderCreate();
    await addAxis("material", "wood, steel, glass", 0);
    expect(screen.getByTestId("variant-total")).toHaveTextContent("3 variants");
    await addAxis("finish", "matte, gloss", 1);
    expect(screen.getByTestId("variant-total")).toHaveTextContent("6 variants");
    expect(screen.getByLabelText("Stage combination formula")).toHaveTextContent("3 × 2");

    // Removing a value recounts immediately.
    await userEvent.click(screen.getByRole("button", { name: "Remove value glass" }));
    expect(screen.getByTestId("variant-total")).toHaveTextContent("4 variants");
  });

  it("refuses a set over the cap before asking the server", async () => {
    mocks.variantCapabilities.mockResolvedValue(caps(4));
    renderCreate();
    await addAxis("material", "wood, steel, glass", 0);
    await addAxis("finish", "matte, gloss", 1);
    expect(screen.getByText(/Over the 4-generation limit/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create set · 6/ })).toBeDisabled();
  });

  it("renders the operation's registry controls, minus the ones the set supplies", async () => {
    renderCreate();
    await screen.findAllByTestId("axis-editor");
    expect(screen.getByRole("group", { name: "Quality" })).toBeInTheDocument();
    expect(screen.queryByText("Seed (-1 random)")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Prompt")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Instruction template")).toBeInTheDocument();
  });

  it("inserts axis placeholders and submits the recipe with every control's value", async () => {
    renderCreate();
    await userEvent.click(await screen.findByRole("button", { name: /add from gallery/i }));
    await userEvent.click(screen.getByRole("button", { name: /Open image/ }));
    await addAxis("material", "wood, steel", 0);
    await addAxis("finish", "matte, gloss, satin", 1);
    const template = screen.getByLabelText("Instruction template");
    await userEvent.type(template, "Rendered in ");
    await userEvent.click(screen.getAllByRole("button", { name: "{{material}}" })[0]);
    expect(template).toHaveValue("Rendered in {{material}}");

    const create = screen.getByRole("button", { name: /Create set · 6/ });
    await waitFor(() => expect(create).toBeEnabled(), { timeout: 3000 });
    await userEvent.click(create);
    await waitFor(() => expect(mocks.createVariantSet).toHaveBeenCalledTimes(1));
    const body = mocks.createVariantSet.mock.calls[0][0];
    expect(body.recipe.sources).toEqual([12]);
    expect(body.recipe.stages[0].axes).toEqual([
      { name: "material", values: ["wood", "steel"] },
      { name: "finish", values: ["matte", "gloss", "satin"] },
    ]);
    expect(body.recipe.stages[0].prompt).toBe("Rendered in {{material}}");
    // Hidden controls still ship their defaults; set-controlled ones never do.
    expect(body.recipe.stages[0].params).toEqual({ quality: "Standard", guidance: 4 });
    expect(body.request_id).toBeTruthy();
    expect(body.collection).toEqual({ mode: "new" });
    expect(await screen.findByText("set page")).toBeInTheDocument();
  });

  it("imports a source straight from this computer", async () => {
    mocks.importAsset.mockResolvedValue({ ...source, id: 77, filename: "photo.png" });
    renderCreate();
    const button = await screen.findByRole("button", { name: /upload a source image/i });
    await userEvent.click(button);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(["png"], "photo.png", { type: "image/png" }));
    expect(mocks.importAsset).toHaveBeenCalledWith(expect.any(File));
    expect(await screen.findByRole("button", { name: "Remove source 77" })).toBeInTheDocument();
    expect(screen.getByText("1/3")).toBeInTheDocument();
  });

  it("makes the mask in one click through the job API", async () => {
    const withInpaint = caps(1000);
    withInpaint.operations = [
      { ...withInpaint.operations[0], kind: "inpaint", title: "Inpaint", mask: "required", max_sources: 1 },
    ];
    mocks.variantCapabilities.mockResolvedValue(withInpaint);
    const mask = { ...source, id: 88, generator: "mask", filename: "mask.png" };
    mocks.createJob.mockResolvedValue({ job_id: 41, job_ids: [41], group_id: null, duplicate: false });
    mocks.waitJob
      .mockResolvedValueOnce({ job: { id: 41, status: "running" }, settled: false, assets: [] })
      .mockResolvedValueOnce({ job: { id: 41, status: "done" }, settled: true, assets: [mask] });
    renderCreate();
    await userEvent.click(await screen.findByRole("button", { name: /add from gallery/i }));
    await userEvent.click(screen.getByRole("button", { name: /Open image/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Select the subject" }));
    expect(mocks.createJob).toHaveBeenCalledWith({
      kind: "matte",
      params: { mode: "mask" },
      inputs: { images: [12] },
    });
    expect(await screen.findByText(/Mask ready/)).toBeInTheDocument();
    expect(mocks.waitJob).toHaveBeenCalledTimes(2);
  });

  it("copies the set as an API request", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderCreate();
    await addAxis("material", "wood, steel", 0);
    await userEvent.click(screen.getByRole("button", { name: /Copy API request/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const text: string = writeText.mock.calls[0][0];
    expect(text).toMatch(/^curl -X POST .*\/api\/variant-sets/);
    const body = JSON.parse(text.slice(text.indexOf("-d '") + 4, text.lastIndexOf("'")));
    expect(body.recipe.stages[0].axes).toEqual([{ name: "material", values: ["wood", "steel"] }]);
  });

  it("duplicates a set that already ran from its recipe snapshot", async () => {
    mocks.variantSet.mockResolvedValue({
      id: 9,
      name: "Finish study",
      recipe: {
        sources: [12],
        seed: { mode: "per_variant", value: 1234 },
        stages: [
          {
            operation: "image_edit",
            prompt: "Make it {{finish}}",
            axes: [{ name: "finish", values: ["matte", "gloss"] }],
            finishing: [{ processor: "background_removal" }],
          },
        ],
      },
    });
    renderCreate("/variants/new?from_set=9");
    expect(await screen.findByDisplayValue("Finish study (again)")).toBeInTheDocument();
    expect(screen.getByLabelText("Instruction template")).toHaveValue("Make it {{finish}}");
    expect(screen.getByTestId("variant-total")).toHaveTextContent("2 variants");
    expect(mocks.variantSet).toHaveBeenCalledWith(9);
  });
});
