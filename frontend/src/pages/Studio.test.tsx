import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PlanRequest } from "../api/types";
import { Studio } from "./Studio";

const presets = [
  {
    id: "custom/storybook",
    revision: 2,
    head_revision: 3,
    name: "Storybook",
    description: "Soft illustrated scenes",
    origin: "custom",
    tags: ["illustration"],
    operation: "text_to_image",
    archived: false,
    client: "mcp",
    definition_hash: "h",
    created_at: "",
    updated_at: "",
  },
  {
    id: "curated/fast",
    revision: 1,
    head_revision: 1,
    name: "Fast general",
    description: "The model's own look",
    origin: "curated",
    tags: ["general"],
    operation: "text_to_image",
    archived: false,
    client: "shipped",
    definition_hash: "g",
    created_at: "",
    updated_at: "",
  },
];

const definitions: Record<string, unknown> = {
  "custom/storybook": {
    name: "Storybook",
    operation: "text_to_image",
    model: { resource: "hf:Tongyi-MAI/Z-Image-Turbo@abc" },
    prompt: {
      template: "a storybook illustration of {{prompt}}, {{styles}}",
      styles: [
        { id: "watercolor", label: "Watercolour", text: "soft watercolour washes" },
        { id: "warm", label: "Warm", text: "warm light" },
      ],
      default_styles: ["watercolor"],
    },
    settings: { aspect: "4:3" },
  },
  "curated/fast": {
    name: "Fast general",
    operation: "text_to_image",
    model: { resource: "hf:Tongyi-MAI/Z-Image-Turbo@abc" },
  },
};

const mocks = vi.hoisted(() => ({
  studioPresets: vi.fn(),
  studioPreset: vi.fn(),
  createStudioPreset: vi.fn(),
  preparePlan: vi.fn(),
  submitPlan: vi.fn(),
  targets: vi.fn(),
  waitJob: vi.fn(),
  toast: vi.fn(),
  blocked: false,
}));

vi.mock("../api/client", () => ({
  isAbort: (error: unknown) => (error as { name?: string } | null)?.name === "AbortError",
  authHeaders: () => ({}),
  ApiError: class extends Error {},
  api: {
    studioPresets: mocks.studioPresets,
    studioPreset: mocks.studioPreset,
    createStudioPreset: mocks.createStudioPreset,
    preparePlan: mocks.preparePlan,
    submitPlan: mocks.submitPlan,
    targets: mocks.targets,
    waitJob: mocks.waitJob,
    loras: () => Promise.resolve({ items: [], max_active: 4, weight_min: -2, weight_max: 2 }),
    cancel: vi.fn(),
    replayJob: vi.fn(),
    asset: vi.fn(() => Promise.reject(new Error("no"))),
  },
}));

const aspect = {
  name: "aspect",
  label: "Aspect",
  type: "aspect",
  default: "1:1",
  options: ["1:1", "4:3", "16:9"],
};
vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      toast: mocks.toast,
      specById: (kind: string) =>
        kind === "image_local"
          ? {
              id: "image_local",
              kind: "image_local",
              controls: [
                { name: "prompt", label: "Prompt", type: "textarea", default: "" },
                aspect,
                { name: "guidance", label: "Guidance", type: "slider", default: 0, min: 0, max: 8 },
              ],
            }
          : undefined,
      presets: null,
      specs: [],
      jobs: {},
      previews: {},
      assets: [],
      controlsRevision: 0,
      assetRevision: 0,
      system: { capability_revision: "cap-1" },
    }),
}));

function planFor(request: PlanRequest) {
  const styles = (request.styles ?? ["watercolor"]).join(", ");
  return {
    dry_run: true,
    ready: !mocks.blocked,
    submittable: !mocks.blocked,
    seed: 101,
    plan: {
      capability_revision: "cap-1",
      kind: "image_local",
      operation: "text_to_image",
      target: {
        requested: "auto",
        resolved: "local",
        reason: "auto: this machine can run it",
        allow_remote: false,
        candidates: [],
      },
      model: {
        resource: "hf:Tongyi-MAI/Z-Image-Turbo@abc",
        repo: "Tongyi-MAI/Z-Image-Turbo",
        revision: "abc",
        family: "zimage",
        verification: "verified",
        known: true,
        variant: "turbo",
        origin: "preset",
      },
      adapters: [],
      prompt: {
        raw: request.prompt,
        template: "",
        styles: [],
        style_ids: [],
        triggers: "",
        notes: "",
        enhancement: { enabled: false, modifications: [] },
        positive: `a storybook illustration of ${request.prompt}, ${styles}`,
        negative: "",
      },
      settings: { aspect: (request.settings?.aspect as string) ?? "4:3", guidance: 0 },
      provenance: {},
      changed_dependencies: [],
      effective_params: { width: 1152, height: 864, steps: 9, guidance: 0, batch: 1 },
      normalized: [],
      inputs: {},
      seed: { base: request.seed ?? 101, origin: "runtime", mode: "increment", items: [101] },
      finish: [],
      validation: [],
      warnings: [],
      readiness: mocks.blocked
        ? {
            state: "blocked",
            ready: false,
            submittable: false,
            blockers: [
              { code: "target_unavailable", message: "the Remote GPU is not connected", blocking: true },
            ],
            actions: [],
          }
        : { state: "ready", ready: true, submittable: true, blockers: [], actions: [] },
      hashes: {
        creative_request_hash: "c",
        execution_plan_hash: `exec-${request.prompt}`,
        material_hash: "m",
      },
    },
  };
}

function renderStudio() {
  return render(
    <MemoryRouter>
      <Studio />
    </MemoryRouter>,
  );
}

describe("the Studio", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.blocked = false;
    mocks.studioPresets.mockResolvedValue(presets);
    mocks.studioPreset.mockImplementation((id: string, revision?: number) =>
      Promise.resolve({
        ...presets.find((p) => p.id === id),
        revision: revision ?? presets.find((p) => p.id === id)!.revision,
        definition: definitions[id],
      }),
    );
    mocks.preparePlan.mockImplementation(async (request: PlanRequest) => planFor(request));
    mocks.submitPlan.mockResolvedValue({ job_id: 31, job_ids: [31], duplicate: false });
    mocks.targets.mockResolvedValue({
      local: { models: [] },
      remote: { models: [] },
      policy: {},
      capability_revision: "cap-1",
    });
    mocks.waitJob.mockResolvedValue({ job: { id: 31, status: "queued" }, settled: false, assets: [] });
  });

  it("a preset and a prompt are enough: the plan shows what runs and Generate submits it", async () => {
    const user = userEvent.setup();
    renderStudio();
    await user.click(await screen.findByRole("option", { name: /Storybook/ }));
    await user.type(screen.getByLabelText("Prompt"), "a lantern festival");
    expect(await screen.findByTestId("composed-prompt")).toHaveTextContent(
      "a storybook illustration of a lantern festival, watercolor",
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(mocks.submitPlan).toHaveBeenCalledTimes(1));
    const [body, hash] = mocks.submitPlan.mock.calls[0];
    expect(body).toMatchObject({
      preset_id: "custom/storybook",
      revision: 2,
      prompt: "a lantern festival",
      seed: 101,
    });
    expect(hash).toBe("exec-a lantern festival");
  });

  it("an AI-saved preset is custom, with its author, never curated", async () => {
    renderStudio();
    const story = await screen.findByRole("option", { name: /Storybook/ });
    expect(within(story).getByText(/Custom · saved by mcp/)).toBeInTheDocument();
    expect(
      within(screen.getByRole("option", { name: /Fast general/ })).getByText("Curated"),
    ).toBeInTheDocument();
  });

  it("a change is shown as customized and Reset restores the preset revision", async () => {
    const user = userEvent.setup();
    renderStudio();
    await user.click(await screen.findByRole("option", { name: /Storybook/ }));
    await user.click(await screen.findByRole("button", { name: "Warm" }));
    expect(screen.getByTestId("customized")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Reset to preset/ }));
    expect(screen.queryByTestId("customized")).not.toBeInTheDocument();
  });

  it("switching presets keeps the prompt, says what did not carry, and can be undone", async () => {
    const user = userEvent.setup();
    renderStudio();
    await user.click(await screen.findByRole("option", { name: /Storybook/ }));
    await user.type(screen.getByLabelText("Prompt"), "a harbour");
    await user.click(await screen.findByRole("button", { name: "Warm" }));
    await user.click(screen.getByRole("button", { name: "Change" }));
    await user.click(await screen.findByRole("option", { name: /Fast general/ }));
    expect(await screen.findByTestId("switch-summary")).toHaveTextContent("Not carried over: styles");
    expect(screen.getByLabelText("Prompt")).toHaveValue("a harbour");
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByTestId("preset-name")).toHaveTextContent("Storybook");
    expect(screen.getByTestId("customized")).toBeInTheDocument();
  });

  it("offers an upgrade when the preset has a newer revision, as a separate step", async () => {
    const user = userEvent.setup();
    renderStudio();
    await user.click(await screen.findByRole("option", { name: /Storybook/ }));
    await user.click(await screen.findByRole("button", { name: "Upgrade to revision 3" }));
    await waitFor(() => expect(mocks.studioPreset).toHaveBeenLastCalledWith("custom/storybook", 3));
  });

  it("a blocked plan cannot be generated, and says why", async () => {
    mocks.blocked = true;
    const user = userEvent.setup();
    renderStudio();
    await user.click(await screen.findByRole("option", { name: /Storybook/ }));
    await user.type(screen.getByLabelText("Prompt"), "x");
    expect(await screen.findByTestId("plan-issues")).toHaveTextContent("the Remote GPU is not connected");
    expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
    expect(screen.getByTestId("submit-blocked")).toHaveTextContent("cannot run here yet");
  });

  it("Ctrl+Enter submits from the compose panel, and the draft survives a reload", async () => {
    const user = userEvent.setup();
    const first = renderStudio();
    await user.click(await screen.findByRole("option", { name: /Storybook/ }));
    await user.type(screen.getByLabelText("Prompt"), "kept");
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled());
    await user.click(screen.getByLabelText("Prompt"));
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(mocks.submitPlan).toHaveBeenCalledTimes(1));
    first.unmount();
    renderStudio();
    expect(await screen.findByLabelText("Prompt")).toHaveValue("kept");
    expect(screen.getByTestId("preset-name")).toHaveTextContent("Storybook");
  });

  it("with no presets, a starter can be created from the configured model", async () => {
    mocks.studioPresets.mockResolvedValue([]);
    mocks.targets.mockResolvedValue({
      local: {
        models: [
          {
            resource: "hf:Tongyi-MAI/Z-Image-Turbo@abc",
            repo: "Tongyi-MAI/Z-Image-Turbo",
            variant: "turbo",
            role: "image",
          },
        ],
      },
      remote: { models: [] },
      policy: {},
      capability_revision: "cap-1",
    });
    mocks.createStudioPreset.mockResolvedValue({
      ...presets[1],
      id: "custom/starter",
      name: "Starter",
      origin: "custom",
    });
    const user = userEvent.setup();
    renderStudio();
    await user.click(await screen.findByRole("button", { name: "Create a starter preset" }));
    await act(async () => {});
    expect(mocks.createStudioPreset).toHaveBeenCalledWith(
      expect.objectContaining({
        operation: "text_to_image",
        model: { resource: "hf:Tongyi-MAI/Z-Image-Turbo@abc" },
      }),
    );
  });
});

describe("a failed job in the session", () => {
  it("says why, from the tip or the error's first line", async () => {
    localStorage.clear();
    mocks.studioPresets.mockResolvedValue(presets);
    mocks.studioPreset.mockImplementation((id: string) =>
      Promise.resolve({ ...presets.find((p) => p.id === id), definition: definitions[id] }),
    );
    mocks.preparePlan.mockImplementation(async (request: PlanRequest) => planFor(request));
    mocks.targets.mockResolvedValue({
      local: { models: [] },
      remote: { models: [] },
      policy: {},
      capability_revision: "cap-1",
    });
    mocks.submitPlan.mockResolvedValue({ job_id: 41, job_ids: [41], duplicate: false });
    mocks.waitJob.mockResolvedValue({
      job: {
        id: 41,
        status: "error",
        tip: "",
        error: "Z-Image-Turbo needs about 15.1 GB of system RAM\nTraceback ...",
      },
      settled: true,
      assets: [],
    });
    const user = userEvent.setup();
    renderStudio();
    await user.click(await screen.findByRole("option", { name: /Storybook/ }));
    await user.type(screen.getByLabelText("Prompt"), "x");
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Generate" }));
    expect(await screen.findByTestId("session-failures")).toHaveTextContent(
      "#41 failed: Z-Image-Turbo needs about 15.1 GB of system RAM",
    );
    expect(screen.getByTestId("session-failures")).not.toHaveTextContent("Traceback");
  });
});
