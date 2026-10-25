import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PresetLibrary } from "./PresetLibrary";

const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    code?: string;
    field?: string;
    constructor(message: string, code?: string, field?: string) {
      super(message);
      this.code = code;
      this.field = field;
    }
  }
  return {
    ApiError,
    studioPresets: vi.fn(),
    studioPreset: vi.fn(),
    updateStudioPreset: vi.fn(),
    createStudioPreset: vi.fn(),
    toast: vi.fn(),
  };
});

vi.mock("../api/client", () => ({
  ApiError: mocks.ApiError,
  api: {
    studioPresets: mocks.studioPresets,
    studioPreset: mocks.studioPreset,
    updateStudioPreset: mocks.updateStudioPreset,
    createStudioPreset: mocks.createStudioPreset,
    cloneStudioPreset: vi.fn(),
    archiveStudioPreset: vi.fn(),
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) => select({ toast: mocks.toast }),
}));

const custom = {
  id: "custom/ink",
  revision: 2,
  head_revision: 2,
  name: "Ink",
  description: "",
  origin: "custom",
  tags: [],
  operation: "text_to_image",
  archived: false,
  client: "ui",
  definition_hash: "h",
  created_at: "",
  updated_at: "",
  definition: {
    name: "Ink",
    operation: "text_to_image",
    model: { resource: "hf:a@b" },
    settings: { aspect: "1:1" },
  },
};
const curated = { ...custom, id: "curated/fast", name: "Fast", origin: "curated", client: "shipped" };

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/presets/*" element={<PresetLibrary />} />
        <Route path="/" element={<div>studio</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("the preset library", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.studioPresets.mockResolvedValue([custom, curated]);
    mocks.studioPreset.mockImplementation((id: string) =>
      Promise.resolve(id === custom.id ? custom : curated),
    );
  });

  it("saves an edited definition against the revision it was opened at", async () => {
    mocks.updateStudioPreset.mockResolvedValue({ ...custom, revision: 3 });
    const user = userEvent.setup();
    renderAt("/presets/custom/ink");
    const editor = await screen.findByLabelText("Preset definition JSON");
    await waitFor(() => expect((editor as HTMLTextAreaElement).value).toContain('"aspect": "1:1"'));
    fireEvent.change(editor, {
      target: { value: JSON.stringify({ ...custom.definition, settings: { aspect: "16:9" } }) },
    });
    await user.click(screen.getByRole("button", { name: "Save revision" }));
    await waitFor(() => expect(mocks.updateStudioPreset).toHaveBeenCalledTimes(1));
    const [id, definition, base] = mocks.updateStudioPreset.mock.calls[0];
    expect(id).toBe("custom/ink");
    expect(definition.settings).toEqual({ aspect: "16:9" });
    expect(base).toBe(2);
  });

  it("a curated preset is read-only", async () => {
    renderAt("/presets/curated/fast");
    expect(await screen.findByRole("button", { name: "Curated presets are read-only" })).toBeDisabled();
    expect(screen.getByLabelText("Preset definition JSON")).toHaveAttribute("readonly");
  });

  it("shows the server's refusal with its field, and offers a copy on a conflict", async () => {
    const user = userEvent.setup();
    mocks.updateStudioPreset.mockRejectedValueOnce(
      new mocks.ApiError("preset 'custom/ink' is at revision 3", "revision_conflict", "base_revision"),
    );
    mocks.updateStudioPreset.mockResolvedValueOnce({
      ...custom,
      id: "custom/ink-copy",
      cloned_from: { id: "custom/ink", revision: 2 },
    });
    renderAt("/presets/custom/ink");
    await screen.findByLabelText("Preset definition JSON");
    await waitFor(() => expect(screen.getByRole("button", { name: "Save revision" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Save revision" }));
    expect(await screen.findByTestId("preset-error")).toHaveTextContent("base_revision");
    await user.click(screen.getByRole("button", { name: "Keep mine as a copy" }));
    await waitFor(() =>
      expect(mocks.updateStudioPreset).toHaveBeenLastCalledWith("custom/ink", expect.anything(), 2, "copy"),
    );
  });

  it("invalid JSON is reported before anything is sent", async () => {
    const user = userEvent.setup();
    renderAt("/presets/custom/ink");
    const editor = await screen.findByLabelText("Preset definition JSON");
    fireEvent.change(editor, { target: { value: "{broken" } });
    await user.click(screen.getByRole("button", { name: "Save revision" }));
    expect(await screen.findByTestId("preset-error")).toHaveTextContent("Not valid JSON");
    expect(mocks.updateStudioPreset).not.toHaveBeenCalled();
  });
});
