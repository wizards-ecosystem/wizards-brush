import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GeneratorView } from "./GeneratorView";

const mocks = vi.hoisted(() => ({
  generate: vi.fn(),
  history: vi.fn(),
  wildcards: vi.fn(),
  toast: vi.fn(),
  refreshJobs: vi.fn(),
  refreshPresets: vi.fn(),
  unavailableReason: "",
}));

vi.mock("../api/client", () => ({
  authHeaders: () => ({}),
  api: {
    generate: mocks.generate,
    history: mocks.history,
    wildcards: mocks.wildcards,
    cancel: vi.fn(),
    createPreset: vi.fn(),
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      specById: (id: string) =>
        id === "image_local"
          ? {
              id: "image_local",
              kind: "image_local",
              title: "Local Image",
              subtitle: "Local image process",
              endpoint: "/api/generate/image-local",
              output: "image",
              needs_image: false,
              needs_remote: false,
              unavailable_reason: mocks.unavailableReason || undefined,
              controls: [
                { name: "prompt", label: "Prompt", type: "textarea", default: "" },
                { name: "batch", label: "Batch", type: "number", default: 1, min: 1, max: 4 },
              ],
            }
          : undefined,
      presets: null,
      system: { remote_gpu: { connected: false } },
      jobs: {},
      previews: {},
      assets: [],
      toast: mocks.toast,
      refreshPresets: mocks.refreshPresets,
      refreshJobs: mocks.refreshJobs,
    }),
}));

function renderGenerator() {
  return render(
    <MemoryRouter initialEntries={["/g/image_local"]}>
      <Routes>
        <Route path="/g/:id" element={<GeneratorView />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("GeneratorView workstation", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.history.mockResolvedValue([]);
    mocks.wildcards.mockResolvedValue([]);
    mocks.generate.mockResolvedValue({ job_id: 42 });
    mocks.unavailableReason = "";
  });

  it("starts in Compose and switches to Session after keyboard submission", async () => {
    const user = userEvent.setup();
    renderGenerator();

    expect(screen.getByRole("button", { name: "Compose" })).toHaveAttribute("aria-pressed", "true");
    await user.type(screen.getByLabelText("Prompt"), "A careful studio portrait");
    await user.keyboard("{Control>}{Enter}{/Control}");

    await waitFor(() => expect(mocks.generate).toHaveBeenCalledTimes(1));
    expect(mocks.generate).toHaveBeenCalledWith(
      "/api/generate/image-local",
      expect.objectContaining({
        prompt: "A careful studio portrait",
        batch: 1,
        request_id: expect.stringMatching(/^[A-Za-z0-9_-]{16,128}$/),
      }),
      expect.any(Object),
    );
    expect(screen.getByRole("button", { name: "Session" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("A clear session canvas")).toBeInTheDocument();
  });

  it("keeps the sticky submission action and exposes validation state", () => {
    renderGenerator();
    expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled();
    expect(screen.getByText("Ctrl / ⌘ + Enter")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Session results" })).toBeInTheDocument();
  });

  it("admits only one request from a same-frame double click", async () => {
    let finish!: (value: { job_id: number }) => void;
    mocks.generate.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const user = userEvent.setup();
    renderGenerator();
    await user.dblClick(screen.getByRole("button", { name: "Generate" }));
    expect(mocks.generate).toHaveBeenCalledTimes(1);
    finish({ job_id: 42 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled());
  });

  it("explains and blocks a generator whose live capability probe failed", () => {
    mocks.unavailableReason = "No CUDA GPU was detected. Connect Remote GPU instead.";
    renderGenerator();

    expect(screen.getByText("No CUDA GPU was detected. Connect Remote GPU instead.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
  });

  it("clears legacy prompt drafts without touching credentials or preferences", () => {
    localStorage.setItem("gen-draft-image_local", JSON.stringify({ prompt: "old private prompt" }));
    localStorage.setItem("api_token", "keep-me");
    localStorage.setItem("gen-control-depth", "full");

    renderGenerator();

    expect(screen.getByLabelText("Prompt")).toHaveValue("");
    expect(localStorage.getItem("gen-draft-image_local")).toBeNull();
    expect(localStorage.getItem("api_token")).toBe("keep-me");
    expect(localStorage.getItem("gen-control-depth")).toBe("full");
  });
});
