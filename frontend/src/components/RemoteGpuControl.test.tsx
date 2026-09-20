import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { RemoteSession } from "../api/types";
import { RemoteGpuControl } from "./RemoteGpuControl";

const session = (over: Partial<RemoteSession> = {}): RemoteSession => ({
  provisioner: "runpod",
  label: "Runpod",
  can_provision: true,
  running: false,
  state: "off",
  detail: "",
  pod_id: "",
  base_url: "",
  gpu: "",
  elapsed_s: 0,
  hourly_usd: 1.39,
  cost_estimate_usd: null,
  worker_connected: false,
  ...over,
});

afterEach(() => vi.restoreAllMocks());

describe("RemoteGpuControl", () => {
  it("is invisible to an install that does not rent hardware", async () => {
    // The self-hosted default must see no trace of a feature it never uses.
    vi.spyOn(api, "remoteSession").mockResolvedValue(
      session({ provisioner: "manual", label: "Self-hosted", can_provision: false }),
    );
    const { container } = render(<RemoteGpuControl />);
    await waitFor(() => expect(api.remoteSession).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("says what it will cost before anything is spent", async () => {
    vi.spyOn(api, "remoteSession").mockResolvedValue(session());
    render(<RemoteGpuControl />);
    expect(await screen.findByRole("button", { name: /start gpu/i })).toBeInTheDocument();
    expect(screen.getByText(/costs nothing while off/i)).toBeInTheDocument();
    expect(screen.getByText(/\$1\.39\/hr/)).toBeInTheDocument();
  });

  it("keeps the running spend on screen", async () => {
    vi.spyOn(api, "remoteSession").mockResolvedValue(
      session({
        running: true, state: "ready", worker_connected: true,
        gpu: "NVIDIA A100-SXM4-80GB", elapsed_s: 1500, cost_estimate_usd: 0.58,
      }),
    );
    render(<RemoteGpuControl />);
    expect(await screen.findByTestId("remote-gpu-cost")).toHaveTextContent("25 min");
    expect(screen.getByTestId("remote-gpu-cost")).toHaveTextContent("~$0.58 so far");
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });

  it("distinguishes a live pod from a worker that can take work", async () => {
    // "Pod is up" and "the model is loaded" are different facts, and conflating
    // them is how a UI claims a GPU is ready while a 58 GB load is still going.
    vi.spyOn(api, "remoteSession").mockResolvedValue(
      session({ running: true, state: "ready", worker_connected: false }),
    );
    render(<RemoteGpuControl />);
    expect(await screen.findByText(/waiting for the worker/i)).toBeInTheDocument();
  });

  it("still offers Stop for a session this install cannot have started", async () => {
    // A pod left over from a previous run must be stoppable even if the
    // provisioner was switched back to manual since.
    vi.spyOn(api, "remoteSession").mockResolvedValue(
      session({ can_provision: false, running: true, state: "error", detail: "the pod is exited" }),
    );
    render(<RemoteGpuControl />);
    expect(await screen.findByRole("button", { name: /stop/i })).toBeInTheDocument();
  });
});
