import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PresetDefinition } from "../../api/types";
import { InputPickers } from "./InputPickers";

const mocks = vi.hoisted(() => ({ referenceSets: vi.fn(), referenceSet: vi.fn() }));

vi.mock("../../api/client", () => ({
  api: {
    referenceSets: mocks.referenceSets,
    referenceSet: mocks.referenceSet,
    asset: vi.fn().mockRejectedValue(new Error("x")),
  },
  authHeaders: () => ({}),
}));

vi.mock("../../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) => select({ assets: [], toast: vi.fn() }),
}));

const definition = (over: Record<string, unknown> = {}) =>
  ({
    name: "Character",
    operation: "edit",
    model: { resource: "hf:a@b" },
    references: [{ role: "character", label: "Character", min: 1, max: 2, ...over }],
  }) as unknown as PresetDefinition;

const read = (count: number) => ({
  id: 4,
  name: "Mara",
  revision: 3,
  head_revision: 3,
  digest: "sha256:x",
  archived: false,
  roles: [],
  client: "",
  created_at: "",
  updated_at: "",
  definition: {
    roles: [{ role: "character", entries: Array.from({ length: count }, (_, i) => ({ asset_id: i + 1 })) }],
  },
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.referenceSets.mockResolvedValue([
    {
      id: 4,
      name: "Mara",
      head_revision: 3,
      archived: false,
      description: "",
      roles: [{ role: "character", label: "", count: 2 }],
      representatives: [],
      updated_at: "",
    },
    {
      id: 5,
      name: "Mug",
      head_revision: 1,
      archived: false,
      description: "",
      roles: [{ role: "product", label: "", count: 1 }],
      representatives: [],
      updated_at: "",
    },
  ]);
});

function renderPickers(props: Partial<Parameters<typeof InputPickers>[0]> = {}) {
  const onReferenceSets = vi.fn();
  const onReferences = vi.fn();
  render(
    <MemoryRouter>
      <InputPickers
        operation="edit"
        definition={definition()}
        images={[]}
        mask={null}
        references={{}}
        referenceSets={{}}
        onImages={vi.fn()}
        onMask={vi.fn()}
        onReferences={onReferences}
        onReferenceSets={onReferenceSets}
        {...props}
      />
    </MemoryRouter>,
  );
  return { onReferenceSets, onReferences };
}

describe("reference roles", () => {
  it("offers only sets that have the role, and choosing one clears picked images", async () => {
    const { onReferenceSets, onReferences } = renderPickers({ references: { character: [9] } });
    const select = await screen.findByLabelText("Character source");
    await waitFor(() => expect(select.querySelectorAll("option")).toHaveLength(2));
    expect(screen.queryByRole("option", { name: "Mug" })).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: "4" } });
    expect(onReferences).toHaveBeenCalledWith({});
    expect(onReferenceSets).toHaveBeenCalledWith({ character: 4 });
  });

  it("previews the set at its revision and says when it does not fit the role", async () => {
    mocks.referenceSet.mockResolvedValue(read(3));
    renderPickers({ referenceSets: { character: 4 } });
    const preview = await screen.findByTestId("set-preview-character");
    await waitFor(() => expect(preview).toHaveTextContent("Mara · revision 3 · 3 references"));
    expect(preview).toHaveTextContent(/takes 1–2; the set is not cut to fit/);
  });

  it("a preset-bound set is shown until the person replaces it", async () => {
    mocks.referenceSet.mockResolvedValue(read(1));
    renderPickers({ definition: definition({ reference_set: 4 }) });
    expect(await screen.findByText(/bound by this preset/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("set-preview-character")).toHaveTextContent("1 reference"));
  });
});
