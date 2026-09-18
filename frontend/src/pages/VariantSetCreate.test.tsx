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

function renderCreate() {
  return render(
    <MemoryRouter initialEntries={["/variants/new"]}>
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
});
