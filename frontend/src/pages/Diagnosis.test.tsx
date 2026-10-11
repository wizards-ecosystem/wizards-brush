import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Diagnosis } from "./Diagnosis";

const mocks = vi.hoisted(() => ({ backends: vi.fn() }));

vi.mock("../api/client", () => ({ api: { backends: mocks.backends } }));

const report = {
  hardware: {
    device: "RTX 4090",
    vram_gb: 24,
    profile: "24gb",
    label: "24 GB or more",
    source: "detected" as const,
    quant: "none",
    offload: false,
    max_side: 1664,
    tier_mp: { Draft: 0.4, High: 1.3 },
    max_batch: 8,
    available: [],
  },
  credentials: { hf_token_set: false },
  backends: [
    {
      id: "local",
      label: "This machine",
      kind: "local" as const,
      health: {
        connected: true,
        reason: "",
        device: "RTX 4090",
        vram_gb: 24,
        vram_used_gb: null,
        build: "",
        build_stale: false,
        queue_depth: 0,
        disk_free_gb: 120,
        features: ["image", "inpaint"],
        models_loaded: [],
      },
      models: [
        {
          id: "org/ready",
          label: "Ready model",
          kind: "image",
          ready: true,
          status: "ready",
          size_gb: 7.2,
          family: "flux",
        },
        {
          id: "org/partial",
          label: "Interrupted model",
          kind: "image",
          ready: false,
          status: "partial",
          size_gb: 2.1,
          family: "sdxl",
        },
        {
          id: "org/new",
          label: "On-demand model",
          kind: "image",
          ready: false,
          status: "absent",
          size_gb: null,
          family: "zimage",
        },
      ],
    },
  ],
};

describe("Diagnosis", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.backends.mockResolvedValue(report);
  });

  it("explains hardware choices and distinguishes model cache states", async () => {
    render(
      <MemoryRouter>
        <Diagnosis />
      </MemoryRouter>,
    );

    expect(await screen.findByText("1 compute lane is ready")).toBeInTheDocument();
    expect(screen.getByText("24 GB or more profile")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Partial download")).toBeInTheDocument();
    expect(screen.getByText("Not downloaded")).toBeInTheDocument();
    expect(screen.getByText(/Public models still work/)).toBeInTheDocument();
  });

  it("shows what a remote worker holds in VRAM, and how to replace a stale one", async () => {
    mocks.backends.mockResolvedValue({
      ...report,
      backends: [
        {
          id: "remote_gpu",
          label: "Remote GPU",
          kind: "remote" as const,
          health: {
            connected: true,
            reason: "",
            device: "NVIDIA A100-SXM4-80GB",
            vram_gb: 79.3,
            vram_used_gb: 56.2,
            build: "deadbeefcafe",
            build_stale: true,
            queue_depth: 0,
            disk_free_gb: 117.1,
            features: ["edit"],
            models_loaded: [],
          },
          models: [],
        },
      ],
    });
    render(
      <MemoryRouter>
        <Diagnosis />
      </MemoryRouter>,
    );

    expect(await screen.findByText("56.2 / 79.3 GB in use")).toBeInTheDocument();
    // The single-file runner is gone; the remedy is a new pod on the current image.
    expect(screen.getByText(/Stop, then Start GPU/)).toBeInTheDocument();
    expect(screen.queryByText(/remote_gpu_filled/)).not.toBeInTheDocument();
  });

  it("refreshes the live report on demand", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Diagnosis />
      </MemoryRouter>,
    );
    await screen.findByText("Ready model");

    await user.click(screen.getByRole("button", { name: "Refresh live status" }));
    await waitFor(() => expect(mocks.backends).toHaveBeenCalledTimes(2));
  });
});
