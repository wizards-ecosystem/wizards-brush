import { act, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GeneratorView } from "./GeneratorView";

/**
 * Section 11 interaction fixtures: state that must survive (ordered finishing,
 * frozen styles) and state that must not leak (a hidden form's shortcut, a
 * late source, a mask painted on the previous image).
 */
const mocks = vi.hoisted(() => ({
  generate: vi.fn(),
  history: vi.fn(),
  wildcards: vi.fn(),
  saveRecipe: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../api/client", () => ({
  authHeaders: () => ({}),
  isAbort: (error: unknown) => (error as { name?: string } | null)?.name === "AbortError",
  api: {
    generate: mocks.generate,
    history: mocks.history,
    wildcards: mocks.wildcards,
    saveRecipe: mocks.saveRecipe,
    cancel: vi.fn(),
    createPreset: vi.fn(),
  },
}));

// The canvas editor needs a real 2D context; stand in a button that "paints".
vi.mock("../components/MaskEditor", () => ({
  MaskEditor: ({ onMask }: { onMask: (file: File | null) => void }) => (
    <button type="button" onClick={() => onMask(new File(["m"], "mask.png", { type: "image/png" }))}>
      Paint mask
    </button>
  ),
}));

const finishing = {
  name: "finish_steps",
  label: "Finishing steps",
  type: "finishing",
  default: [],
  processors: [
    {
      name: "background_removal",
      label: "Remove background",
      description: "",
      available: true,
      unavailable_reason: null,
      options: [],
    },
    {
      name: "resize",
      label: "Resize",
      description: "",
      available: true,
      unavailable_reason: null,
      options: [
        { name: "width", label: "Width", type: "number", default: 1024, min: 1, max: 8192 },
        { name: "height", label: "Height", type: "number", default: 1024, min: 1, max: 8192 },
        {
          name: "mode",
          label: "Fit",
          type: "segmented",
          default: "contain",
          options: ["contain", "cover", "stretch"],
        },
        {
          name: "background",
          label: "Padding",
          type: "select",
          default: "transparent",
          options: ["transparent", "#ffffff", "#000000"],
        },
      ],
    },
  ],
};

const prompt = { name: "prompt", label: "Prompt", type: "textarea", default: "", styles: true };
const specs: Record<string, unknown> = {
  image_local: {
    id: "image_local",
    kind: "image_local",
    title: "Local Image",
    subtitle: "",
    endpoint: "/api/generate/image/local",
    output: "image",
    needs_image: false,
    needs_remote: false,
    controls: [prompt, finishing],
  },
  img2img: {
    id: "img2img",
    kind: "img2img",
    title: "Img2Img",
    subtitle: "",
    endpoint: "/api/generate/image/img2img",
    output: "image",
    needs_image: true,
    needs_remote: false,
    controls: [prompt],
  },
  inpaint: {
    id: "inpaint",
    kind: "inpaint",
    title: "Inpaint",
    subtitle: "",
    endpoint: "/api/generate/image/inpaint",
    output: "image",
    needs_image: true,
    needs_mask: true,
    needs_remote: false,
    controls: [prompt],
  },
};

const presets = {
  style_profiles: [
    {
      category: "Paint",
      items: [{ id: "watercolor", label: "Watercolour", text: "soft watercolour washes" }],
    },
  ],
  user: [],
};

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      specById: (id: string) => specs[id],
      presets,
      system: { remote_gpu: { connected: false } },
      jobs: {},
      previews: {},
      assets: [],
      toast: mocks.toast,
      refreshPresets: vi.fn(),
      refreshJobs: vi.fn(),
    }),
}));

const nav: { go: (state: unknown) => void } = { go: () => {} };
function Navigator({ path }: { path: string }) {
  const navigate = useNavigate();
  useEffect(() => {
    nav.go = (state) => navigate(path, { state });
  }, [navigate, path]);
  return null;
}

function renderAt(path: string, state?: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: path, state }]}>
      <Navigator path={path} />
      <Routes>
        <Route path="/g/:id" element={<GeneratorView />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("authoring state", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.history.mockResolvedValue([]);
    mocks.wildcards.mockResolvedValue([]);
    mocks.generate.mockResolvedValue({ job_id: 1 });
    mocks.saveRecipe.mockResolvedValue({ name: "x", slug: "x" });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("restores an ordered finishing chain from a draft and sends it unchanged", async () => {
    const chain = [
      { processor: "background_removal" },
      { processor: "resize", width: 640, height: 360, mode: "cover", background: "#000000" },
    ];
    localStorage.setItem("wizard-brush-draft-epoch", "3");
    localStorage.setItem("gen-draft-3-image_local", JSON.stringify({ prompt: "a cup", finish_steps: chain }));
    const user = userEvent.setup();
    renderAt("/g/image_local");
    expect(screen.queryByText(/Updated stale saved settings/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(mocks.generate).toHaveBeenCalledTimes(1));
    expect(mocks.generate.mock.calls[0][1].finish_steps).toEqual(chain);
  });

  it("recalls a finishing chain from history with its order and values", async () => {
    const chain = [
      { processor: "resize", width: 300, height: 200, mode: "stretch", background: "#ffffff" },
      { processor: "background_removal" },
    ];
    mocks.history.mockResolvedValue([
      {
        id: 1,
        kind: "image_local",
        prompt: "a lamp",
        negative: "",
        favorite: false,
        created_at: "",
        params: { prompt: "a lamp", raw_prompt: "a lamp", finish_steps: chain },
      },
    ]);
    const user = userEvent.setup();
    renderAt("/g/image_local");
    await user.click(screen.getByRole("button", { name: "Recall a recent prompt" }));
    await user.click(await screen.findByRole("button", { name: "a lamp" }));
    await user.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(mocks.generate).toHaveBeenCalledTimes(1));
    expect(mocks.generate.mock.calls[0][1].finish_steps).toEqual(chain);
  });

  it("Ctrl+Enter inside the Save recipe dialog never submits the generator behind it", async () => {
    const user = userEvent.setup();
    renderAt("/g/image_local");
    await user.type(screen.getByLabelText("Prompt"), "a teapot");
    await user.click(screen.getByRole("button", { name: "Save as recipe" }));
    await user.type(screen.getByLabelText("Name"), "Teapot");
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(mocks.generate).not.toHaveBeenCalled();
    // The shortcut still works from the form itself.
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByLabelText("Prompt"));
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(mocks.generate).toHaveBeenCalledTimes(1));
  });

  it("Ctrl+Enter from outside the compose panel does nothing", async () => {
    const user = userEvent.setup();
    renderAt("/g/image_local");
    await user.type(screen.getByLabelText("Prompt"), "a teapot");
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("saves the selected style text with the raw subject", async () => {
    const user = userEvent.setup();
    renderAt("/g/image_local", { prefillValues: { prompt: "a fox", style_ids: ["watercolor"] } });
    await user.click(screen.getByRole("button", { name: "Save as recipe" }));
    await user.type(screen.getByLabelText("Name"), "Fox");
    await user.click(screen.getByRole("button", { name: "Save recipe" }));
    await waitFor(() => expect(mocks.saveRecipe).toHaveBeenCalledTimes(1));
    const body = mocks.saveRecipe.mock.calls[0][0];
    expect(body.params.prompt).toBe("a fox");
    expect(body.styles).toEqual([{ id: "watercolor", text: "soft watercolour washes" }]);
  });

  it("a late source A never replaces a newer source B", async () => {
    const pending: Record<string, (blob: Blob) => void> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (url: string, init?: RequestInit) =>
          new Promise((resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
            );
            pending[url] = (blob) => resolve({ ok: true, blob: () => Promise.resolve(blob) });
          }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/g/img2img", { fromAsset: { url: "/a", filename: "a.png" } });
    act(() => nav.go({ fromAsset: { url: "/b", filename: "b.png" } }));
    await act(async () => pending["/b"](new Blob(["b"], { type: "image/png" })));
    await act(async () => pending["/a"]?.(new Blob(["a"], { type: "image/png" })));
    await user.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(mocks.generate).toHaveBeenCalledTimes(1));
    expect(mocks.generate.mock.calls[0][2].image.name).toBe("b.png");
    expect(mocks.toast).not.toHaveBeenCalledWith("Couldn't load the source image", "error");
  });

  it("leaving while a source loads neither errors nor updates the old form", async () => {
    let resolveIt: (value: unknown) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise((resolve) => (resolveIt = resolve))),
    );
    const { unmount } = renderAt("/g/img2img", { fromAsset: { url: "/a", filename: "a.png" } });
    unmount();
    await act(async () => resolveIt({ ok: true, blob: () => Promise.resolve(new Blob(["a"])) }));
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("replacing the source image clears a mask painted on the old one", async () => {
    const user = userEvent.setup();
    const { container } = renderAt("/g/inpaint");
    const input = () => container.querySelector('input[type="file"][accept="image/*"]') as HTMLInputElement;
    await user.upload(input(), new File(["a"], "a.png", { type: "image/png" }));
    await user.click(screen.getByRole("button", { name: "Paint mask" }));
    expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled();
    await user.upload(input(), new File(["b"], "b.png", { type: "image/png" }));
    expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
  });

  it("says when reused settings came only from image metadata", () => {
    renderAt("/g/image_local", {
      prefillValues: { prompt: "a fox" },
      reuseNote: "Partial reuse from image metadata; not recorded: adapters, finishing steps.",
    });
    expect(screen.getByTestId("reuse-note")).toHaveTextContent("not recorded: adapters, finishing steps");
  });
});

describe("controls refetched while open", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.history.mockResolvedValue([]);
    mocks.wildcards.mockResolvedValue([]);
    mocks.generate.mockResolvedValue({ job_id: 1 });
  });

  it("keeps the draft, drops a control the install no longer offers, and says so", async () => {
    const original = specs.image_local as { controls: unknown[] };
    const speed = { name: "speed_mode", label: "Speed mode", type: "toggle", default: false };
    specs.image_local = { ...original, controls: [...original.controls, speed] };
    const user = userEvent.setup();
    const view = render(
      <MemoryRouter initialEntries={["/g/image_local"]}>
        <Routes>
          <Route path="/g/:id" element={<GeneratorView />} />
        </Routes>
      </MemoryRouter>,
    );
    await user.type(screen.getByLabelText("Prompt"), "a quiet harbour");
    await user.click(screen.getByLabelText("Speed mode"));
    // The worker that offered speed mode went away: the registry is refetched.
    specs.image_local = { ...original };
    view.rerender(
      <MemoryRouter initialEntries={["/g/image_local"]}>
        <Routes>
          <Route path="/g/:id" element={<GeneratorView />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("capability-note")).toHaveTextContent("speed_mode");
    expect(screen.getByLabelText("Prompt")).toHaveValue("a quiet harbour");
    await user.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(mocks.generate).toHaveBeenCalledTimes(1));
    expect(mocks.generate.mock.calls[0][1]).not.toHaveProperty("speed_mode");
    specs.image_local = original;
  });
});
