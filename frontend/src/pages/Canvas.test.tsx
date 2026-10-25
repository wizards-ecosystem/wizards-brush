import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Canvas } from "./Canvas";

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
    canvases: vi.fn(),
    canvas: vi.fn(),
    saveCanvas: vi.fn(),
    flattenCanvas: vi.fn(),
    createCanvas: vi.fn(),
    toast: vi.fn(),
    assets: [
      { id: 1, kind: "image", generator: "import", width: 64, height: 32, url: "/a1", thumb_url: "/t1" },
      { id: 2, kind: "image", generator: "import", width: 16, height: 16, url: "/a2", thumb_url: "/t2" },
    ],
  };
});

vi.mock("../api/client", () => ({
  ApiError: mocks.ApiError,
  api: {
    canvases: mocks.canvases,
    canvas: mocks.canvas,
    saveCanvas: mocks.saveCanvas,
    flattenCanvas: mocks.flattenCanvas,
    createCanvas: mocks.createCanvas,
    deleteCanvas: vi.fn(),
    asset: vi.fn().mockRejectedValue(new Error("not found")),
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

const project = (layers: unknown[] = [], missing: number[] = []) => ({
  id: 3,
  name: "Poster",
  revision: 4,
  head_revision: 4,
  digest: "sha256:x",
  archived: false,
  missing,
  client: "ui",
  created_at: "",
  updated_at: "",
  document: {
    schema_version: 1,
    width: 128,
    height: 64,
    color_space: "srgb",
    background: "transparent",
    layers,
  },
});

const layer = (id: string, asset_id: number, extra: Record<string, unknown> = {}) => ({
  id,
  name: id,
  asset_id,
  mask_id: null,
  transform: { x: 0, y: 0, scale_x: 1, scale_y: 1, rotation: 0 },
  opacity: 1,
  visible: true,
  blend_mode: "normal",
  ...extra,
});

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/canvas" element={<Canvas />} />
        <Route path="/canvas/:id" element={<Canvas />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.canvases.mockResolvedValue([]);
});

afterEach(() => vi.useRealTimers());

describe("Canvas", () => {
  it("adds a layer, autosaves from the opened revision and keeps undo", async () => {
    mocks.canvas.mockResolvedValue(project());
    mocks.saveCanvas.mockImplementation(async (_id, document) => ({
      ...project(document.layers),
      revision: 5,
      head_revision: 5,
    }));
    renderAt("/canvas/3");
    fireEvent.click(await screen.findByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: "pick 1" }));
    expect(screen.getByRole("button", { name: "Layer Asset 1" })).toBeInTheDocument();
    await waitFor(() => expect(mocks.saveCanvas).toHaveBeenCalled(), { timeout: 3000 });
    const [id, document, base] = mocks.saveCanvas.mock.calls[0];
    expect([id, base]).toEqual([3, 4]);
    expect(document.layers[0]).toMatchObject({ id: "layer-1", asset_id: 1 });
    expect(await screen.findByTestId("canvas-save-state")).toHaveTextContent("saved · revision 5");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.queryByRole("button", { name: "Layer Asset 1" })).not.toBeInTheDocument();
  });

  it("a conflicting save is reported and never retried over the other edit", async () => {
    mocks.canvas.mockResolvedValue(project([layer("a", 1)]));
    mocks.saveCanvas.mockRejectedValue(
      new mocks.ApiError("conflict", "revision_conflict", { current_revision: 6 }),
    );
    renderAt("/canvas/3");
    await screen.findByTestId("canvas-inspector");
    fireEvent.change(screen.getByLabelText("X"), { target: { value: "12" } });
    expect(await screen.findByTestId("canvas-conflict", {}, { timeout: 3000 })).toHaveTextContent(
      "Revision 6",
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1200));
    });
    expect(mocks.saveCanvas).toHaveBeenCalledTimes(1);
  });

  it("shows a missing source as a placeholder instead of failing", async () => {
    mocks.canvas.mockResolvedValue(project([layer("gone", 9)], [9]));
    renderAt("/canvas/3");
    expect(await screen.findByText("missing #9")).toBeInTheDocument();
    expect(screen.getByTestId("canvas-layers")).toHaveTextContent("(missing)");
  });

  it("a mask must match its layer's image", async () => {
    mocks.canvas.mockResolvedValue(project([layer("a", 1)]));
    renderAt("/canvas/3");
    fireEvent.click(await screen.findByRole("button", { name: "Set mask" }));
    fireEvent.click(screen.getByRole("button", { name: "pick 2" }));
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringMatching(/same size/), "error");
  });
});
