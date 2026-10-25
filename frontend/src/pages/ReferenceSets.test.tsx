import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReferenceSets } from "./ReferenceSets";

const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    code?: string;
    state?: Record<string, unknown>;
    constructor(message: string, code?: string, state?: Record<string, unknown>) {
      super(message);
      this.code = code;
      this.state = state;
    }
  }
  return {
    ApiError,
    referenceSets: vi.fn(),
    referenceSet: vi.fn(),
    createReferenceSet: vi.fn(),
    updateReferenceSet: vi.fn(),
    toast: vi.fn(),
    assets: [
      { id: 1, kind: "image", generator: "local_image", width: 64, height: 64, url: "/a1", thumb_url: "/t1" },
      { id: 2, kind: "image", generator: "local_image", width: 64, height: 64, url: "/a2", thumb_url: "/t2" },
    ],
  };
});

vi.mock("../api/client", () => ({
  ApiError: mocks.ApiError,
  api: {
    referenceSets: mocks.referenceSets,
    referenceSet: mocks.referenceSet,
    createReferenceSet: mocks.createReferenceSet,
    updateReferenceSet: mocks.updateReferenceSet,
    archiveReferenceSet: vi.fn(),
    asset: vi.fn(),
  },
}));

vi.mock("../components/GalleryPicker", () => ({
  GalleryPicker: ({ onPick }: { onPick: (a: unknown) => void }) => (
    <div>
      {mocks.assets.map((a) => (
        <button key={a.id} type="button" onClick={() => onPick(a)}>
          pick {a.id}
        </button>
      ))}
    </div>
  ),
}));

vi.mock("../store/useStore", () => {
  const state = { toast: mocks.toast, assets: mocks.assets };
  const useStore = (select: (s: unknown) => unknown) => select(state);
  useStore.getState = () => state;
  return { useStore };
});

const stored = {
  id: 7,
  name: "Mara",
  revision: 2,
  head_revision: 2,
  digest: "sha256:abcdef0123456789abcdef",
  archived: false,
  roles: [],
  client: "ui",
  created_at: "",
  updated_at: "",
  definition: {
    notes: "red scarf",
    roles: [{ role: "character", label: "Mara", entries: [{ asset_id: 1 }, { asset_id: 2 }] }],
    representatives: [],
  },
};

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/references" element={<ReferenceSets />} />
        <Route path="/references/:id" element={<ReferenceSets />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.referenceSets.mockResolvedValue([]);
  mocks.referenceSet.mockResolvedValue(stored);
});

describe("ReferenceSets", () => {
  it("creates a set from gallery images in the order they were added", async () => {
    mocks.createReferenceSet.mockResolvedValue({ ...stored, revision: 1, head_revision: 1 });
    renderAt("/references/new");
    const create = await screen.findByRole("button", { name: /create set/i });
    expect(create).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Mara" } });
    for (const id of [2, 1]) {
      fireEvent.click(screen.getByRole("button", { name: /add from gallery/i }));
      fireEvent.click(await screen.findByRole("button", { name: `pick ${id}` }));
    }
    fireEvent.click(screen.getByRole("button", { name: /create set/i }));
    await waitFor(() => expect(mocks.createReferenceSet).toHaveBeenCalled());
    const [, definition] = mocks.createReferenceSet.mock.calls[0];
    expect(definition.roles[0].entries.map((e: { asset_id: number }) => e.asset_id)).toEqual([2, 1]);
  });

  it("reorders entries and saves from the revision it opened", async () => {
    mocks.updateReferenceSet.mockResolvedValue({ ...stored, revision: 3, head_revision: 3 });
    renderAt("/references/7");
    await screen.findAllByTestId("reference-entry");
    fireEvent.click(screen.getByRole("button", { name: "Move reference 2 up" }));
    fireEvent.click(screen.getByRole("button", { name: /save as revision 3/i }));
    await waitFor(() => expect(mocks.updateReferenceSet).toHaveBeenCalled());
    const [id, definition, base] = mocks.updateReferenceSet.mock.calls[0];
    expect(id).toBe(7);
    expect(base).toBe(2);
    expect(definition.roles[0].entries.map((e: { asset_id: number }) => e.asset_id)).toEqual([2, 1]);
  });

  it("a stale save says so and overwrites nothing", async () => {
    mocks.updateReferenceSet.mockRejectedValue(
      new mocks.ApiError("conflict", "revision_conflict", { current_revision: 4 }),
    );
    renderAt("/references/7");
    fireEvent.click(await screen.findByRole("button", { name: /save as revision 3/i }));
    expect(await screen.findByTestId("reference-conflict")).toHaveTextContent("revision 4");
  });
});
