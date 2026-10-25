import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
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

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

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
        running: true,
        state: "ready",
        worker_connected: true,
        gpu: "NVIDIA A100-SXM4-80GB",
        elapsed_s: 1500,
        cost_estimate_usd: 0.58,
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

  it("keeps polling a ready pod, so a pod stopped elsewhere disappears", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const read = vi
      .spyOn(api, "remoteSession")
      .mockResolvedValueOnce(session({ running: true, state: "ready", worker_connected: true }))
      .mockResolvedValue(session());
    render(<RemoteGpuControl />);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(31000);
    });
    expect(read.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(await screen.findByText(/costs nothing while off/i)).toBeInTheDocument();
  });

  it("an unconfirmed stop stays on screen as possibly billing, with retry and forget", async () => {
    vi.spyOn(api, "remoteSession").mockResolvedValue(
      session({ running: true, state: "ready", worker_connected: true, pod_id: "pod-abc", elapsed_s: 120 }),
    );
    const left = session({
      running: true,
      state: "stop_unconfirmed",
      pod_id: "pod-abc",
      possibly_billing: true,
      stop_error: "Runpod did not answer the terminate request",
      stop_attempts: 1,
      elapsed_s: 130,
    });
    vi.spyOn(api, "stopRemoteSession").mockRejectedValue(
      new ApiError(502, { code: "stop_unconfirmed", state: left as unknown as Record<string, unknown> }, "x"),
    );
    const forget = vi.spyOn(api, "forgetRemoteSession").mockResolvedValue(session());
    render(<RemoteGpuControl />);
    fireEvent.click(await screen.findByRole("button", { name: /^stop$/i }));
    expect(await screen.findByTestId("remote-gpu-unresolved")).toHaveTextContent(/did not answer/);
    expect(screen.getByTestId("remote-gpu-cost")).toHaveTextContent(/may still be billing/);
    expect(screen.getByRole("button", { name: /retry stop/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /forget it/i }));
    const confirm = screen.getByRole("button", { name: /forget pod/i });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/type the pod id/i), { target: { value: "pod-abc" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(forget).toHaveBeenCalledWith("pod-abc"));
    expect(await screen.findByText(/costs nothing while off/i)).toBeInTheDocument();
  });

  it("a slow poll cannot overwrite a newer answer", async () => {
    let release: (value: RemoteSession) => void = () => {};
    vi.spyOn(api, "remoteSession")
      .mockResolvedValueOnce(session({ running: true, state: "ready", worker_connected: true }))
      .mockImplementationOnce(() => new Promise((resolve) => (release = resolve)));
    vi.spyOn(api, "stopRemoteSession").mockResolvedValue(session());
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<RemoteGpuControl />);
    await screen.findByText("Ready");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(31000); // the slow poll is now in flight
    });
    fireEvent.click(screen.getByRole("button", { name: /^stop$/i }));
    expect(await screen.findByText(/costs nothing while off/i)).toBeInTheDocument();
    await act(async () => release(session({ running: true, state: "ready", worker_connected: true })));
    expect(screen.getByText(/costs nothing while off/i)).toBeInTheDocument();
  });
});
