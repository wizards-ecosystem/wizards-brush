import { describe, expect, it } from "vitest";
import type { ExecutionPlan, PreparedPlan } from "../api/types";
import {
  canSubmit,
  effectiveRequest,
  initialPreparedRun,
  preparedRunReducer,
  staleReason,
  type PreparedRunState,
} from "./preparedRun";

function plan(overrides: Partial<ExecutionPlan> = {}): ExecutionPlan {
  return {
    capability_revision: "cap-1",
    readiness: { state: "ready", ready: true, submittable: true, blockers: [], actions: [] },
    hashes: { creative_request_hash: "c", execution_plan_hash: "e", material_hash: "m" },
    ...overrides,
  } as ExecutionPlan;
}

function prepared(p: ExecutionPlan, seed = 42): PreparedPlan {
  return { dry_run: true, ready: p.readiness.ready, submittable: p.readiness.submittable, seed, plan: p };
}

const run = (state: PreparedRunState, ...actions: Parameters<typeof preparedRunReducer>[1][]) =>
  actions.reduce(preparedRunReducer, state);

describe("prepared runs", () => {
  it("accepts a preview only for the draft it was asked for", () => {
    let s = run(initialPreparedRun, { type: "draft", request: { preset_id: "a", prompt: "one" } });
    const first = s.revision;
    s = run(s, { type: "draft", request: { preset_id: "a", prompt: "two" } });
    s = run(s, { type: "previewOk", revision: first, prepared: prepared(plan()) }); // late answer
    expect(s.preview.plan).toBeNull();
    s = run(s, { type: "previewOk", revision: s.revision, prepared: prepared(plan()) });
    expect(s.preview.plan).not.toBeNull();
    expect(canSubmit(s)).toBe(true);
  });

  it("a plan stops being submittable when the draft moves on", () => {
    let s = run(initialPreparedRun, { type: "draft", request: { prompt: "a" } });
    s = run(s, { type: "previewOk", revision: s.revision, prepared: prepared(plan()) });
    s = run(s, { type: "draft", request: { prompt: "b" } });
    expect(staleReason(s)).toBe("draft-changed");
  });

  it("a plan resolved against older capabilities cannot be submitted", () => {
    let s = run(
      initialPreparedRun,
      { type: "capabilities", revision: "cap-1" },
      { type: "draft", request: { prompt: "a" } },
    );
    s = run(s, { type: "previewOk", revision: s.revision, prepared: prepared(plan()) });
    expect(canSubmit(s)).toBe(true);
    s = run(s, { type: "capabilities", revision: "cap-2" });
    expect(staleReason(s)).toBe("capabilities-changed");
  });

  it("server readiness decides: a blocked plan is never submittable", () => {
    let s = run(initialPreparedRun, { type: "draft", request: { prompt: "a" } });
    const blocked = plan({
      readiness: { state: "blocked", ready: false, submittable: false, blockers: [], actions: [] },
    });
    s = run(s, { type: "previewOk", revision: s.revision, prepared: prepared(blocked) });
    expect(staleReason(s)).toBe("blocked");
  });

  it("a failed submission keeps the draft and its plan", () => {
    let s = run(initialPreparedRun, { type: "draft", request: { preset_id: "a", prompt: "keep me" } });
    s = run(s, { type: "previewOk", revision: s.revision, prepared: prepared(plan()) });
    s = run(
      s,
      { type: "submitStart" },
      { type: "submitError", error: { detail: "Can't reach the backend" } },
    );
    expect(s.request?.prompt).toBe("keep me");
    expect(s.preview.plan).not.toBeNull();
    expect(s.submit.status).toBe("error");
    expect(canSubmit(s)).toBe(true); // it can be retried as is
  });

  it("pins the server's random seed until a new seed is asked for", () => {
    let s = run(initialPreparedRun, { type: "draft", request: { prompt: "a" } });
    s = run(s, { type: "previewOk", revision: s.revision, prepared: prepared(plan(), 77) });
    expect(effectiveRequest(s)?.seed).toBe(77);
    s = run(s, { type: "newSeed" });
    expect(effectiveRequest(s)?.seed).toBeUndefined();
    const explicit = run(initialPreparedRun, { type: "draft", request: { prompt: "a", seed: 5 } });
    expect(effectiveRequest(explicit)?.seed).toBe(5);
  });
});
