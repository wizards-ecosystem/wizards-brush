import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Settings } from "./Settings";

const mocks = vi.hoisted(() => ({
  getSettings: vi.fn(),
  targets: vi.fn(),
  setRoutingPolicy: vi.fn(),
  saveBrowserSession: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    getSettings: mocks.getSettings,
    userPresets: vi.fn().mockResolvedValue([]),
    saveSettings: vi.fn(),
    deletePreset: vi.fn(),
    targets: mocks.targets,
    setRoutingPolicy: mocks.setRoutingPolicy,
    wildcards: vi.fn().mockResolvedValue([]),
  },
  saveBrowserSession: mocks.saveBrowserSession,
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      refreshSystem: vi.fn().mockResolvedValue(undefined),
      system: null,
      refreshPresets: vi.fn().mockResolvedValue(undefined),
      stats: null,
      refreshStats: vi.fn().mockResolvedValue(undefined),
      toast: mocks.toast,
      refreshControls: vi.fn().mockResolvedValue(undefined),
    }),
}));

describe("Settings browser access", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getSettings.mockRejectedValue(new Error("unauthorized"));
    mocks.saveBrowserSession.mockImplementation(() => new Promise(() => undefined));
  });

  it("keeps the token form available when protected settings cannot load", async () => {
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <Settings />
      </MemoryRouter>,
    );

    expect(await screen.findByText(/protected settings could not be loaded/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("API token"), { target: { value: "new token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));

    expect(mocks.saveBrowserSession).toHaveBeenCalledWith("new token");
  });
});

describe("Settings routing policy", () => {
  const report = (target: "local" | "remote", over: Record<string, unknown> = {}) => ({
    target,
    identity: target === "local" ? "local:abc" : "remote:none",
    confidence: "measured",
    hardware: {},
    operations: ["text_to_image"],
    kinds: [],
    models: [],
    observed_at: 0,
    ...over,
  });
  const view = (policy: Record<string, boolean>) => ({
    local: report("local"),
    remote: report("remote", { connected: false, configured_operations: ["edit", "text_to_image"] }),
    policy,
    capability_revision: "0123456789abcdef",
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getSettings.mockResolvedValue({
      remote_gpu_base_url: "",
      embed_metadata: true,
      embed_provenance: true,
    });
  });

  it("shows the policy and both targets, and saves one change at a time", async () => {
    const policy = { remote_enabled: true, auto_may_use_remote: false, send_inputs_to_remote: false };
    mocks.targets
      .mockResolvedValueOnce(view(policy))
      .mockResolvedValue(view({ ...policy, auto_may_use_remote: true }));
    mocks.setRoutingPolicy.mockResolvedValue({ ...policy, auto_may_use_remote: true });
    render(
      <MemoryRouter>
        <Settings />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("target-summary")).toHaveTextContent(
      "Not connected · 2 operations configured",
    );
    const auto = screen.getByRole("checkbox", { name: /auto may choose/i });
    expect(auto).not.toBeChecked();
    fireEvent.click(auto);
    expect(mocks.setRoutingPolicy).toHaveBeenCalledWith({ auto_may_use_remote: true });
    expect(await screen.findByRole("checkbox", { name: /auto may choose/i })).toBeChecked();
  });
});
