import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExecutionPlan, PlanRequest, PreparedPlan } from "../api/types";

const mocks = vi.hoisted(() => ({
  preparePlan: vi.fn(),
  submitPlan: vi.fn(),
  capability: "cap-1" as string | null,
}));

vi.mock("../api/client", () => ({
  api: { preparePlan: mocks.preparePlan, submitPlan: mocks.submitPlan },
  isAbort: (error: unknown) => (error as { name?: string } | null)?.name === "AbortError",
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({ system: { capability_revision: mocks.capability } }),
}));

import { usePreparedRun } from "./usePreparedRun";

function answer(request: PlanRequest, capability = "cap-1", seed = 9): PreparedPlan {
  return {
    dry_run: true,
    ready: true,
    submittable: true,
    seed,
    plan: {
      capability_revision: capability,
      prompt: { raw: request.prompt ?? "" },
      readiness: { state: "ready", ready: true, submittable: true, blockers: [], actions: [] },
      hashes: {
        creative_request_hash: "c",
        execution_plan_hash: `exec-${request.prompt}`,
        material_hash: "m",
      },
    } as unknown as ExecutionPlan,
  };
}

describe("usePreparedRun", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.preparePlan.mockReset();
    mocks.submitPlan.mockReset();
    mocks.capability = "cap-1";
  });
  afterEach(() => vi.useRealTimers());

  it("debounces, cancels the older preview and ignores its late answer", async () => {
    const pending: (() => void)[] = [];
    mocks.preparePlan.mockImplementation(
      (request: PlanRequest) => new Promise((resolve) => pending.push(() => resolve(answer(request)))),
    );
    const { result, rerender } = renderHook(({ request }) => usePreparedRun(request, { debounceMs: 50 }), {
      initialProps: { request: { preset_id: "p", prompt: "first" } as PlanRequest },
    });
    await act(async () => vi.advanceTimersByTimeAsync(60));
    rerender({ request: { preset_id: "p", prompt: "second" } });
    await act(async () => vi.advanceTimersByTimeAsync(60));
    expect(mocks.preparePlan).toHaveBeenCalledTimes(2);
    expect(mocks.preparePlan.mock.calls[0][1].aborted).toBe(true); // the older fetch was canceled
    await act(async () => pending[0]()); // its answer still arrives, late
    expect(result.current.state.preview.plan).toBeNull();
    await act(async () => pending[1]());
    expect(result.current.state.preview.plan?.prompt.raw).toBe("second");
    expect(result.current.canSubmit).toBe(true);
  });

  it("previews again when the install's capabilities change, and is stale until then", async () => {
    mocks.preparePlan.mockImplementation(async (request: PlanRequest) => answer(request, mocks.capability!));
    const { result, rerender } = renderHook(() =>
      usePreparedRun({ preset_id: "p", prompt: "x" }, { debounceMs: 10 }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(20));
    await waitFor(() => expect(result.current.canSubmit).toBe(true));
    mocks.capability = "cap-2";
    rerender();
    expect(result.current.stale).toBe("capabilities-changed");
    await act(async () => vi.advanceTimersByTimeAsync(20));
    await waitFor(() => expect(result.current.canSubmit).toBe(true));
    expect(mocks.preparePlan).toHaveBeenCalledTimes(2);
  });

  it("submits the previewed hash with one request id, and keeps the draft when it fails", async () => {
    mocks.preparePlan.mockImplementation(async (request: PlanRequest) => answer(request));
    mocks.submitPlan.mockRejectedValueOnce(new Error("Can't reach the backend — is it running?"));
    mocks.submitPlan.mockResolvedValueOnce({ job_id: 5, job_ids: [5], duplicate: false });
    const { result } = renderHook(() =>
      usePreparedRun({ preset_id: "p", prompt: "keep" }, { debounceMs: 10 }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(20));
    await waitFor(() => expect(result.current.canSubmit).toBe(true));
    await act(async () => {
      await result.current.submit();
    });
    expect(result.current.state.submit.status).toBe("error");
    expect(result.current.state.request?.prompt).toBe("keep");
    await act(async () => {
      await result.current.submit();
    });
    expect(result.current.state.submit.jobIds).toEqual([5]);
    const [first, second] = mocks.submitPlan.mock.calls;
    expect(first[1]).toBe("exec-keep");
    expect(first[0].seed).toBe(9); // the seed the preview chose
    expect(second[2]).toBe(first[2]); // the retry reused its request id
  });
});
