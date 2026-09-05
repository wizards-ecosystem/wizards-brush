import { describe, expect, it } from "vitest";
import type { Job } from "../api/types";
import { LOAD_STAGE_LABEL, applyJobEvent, loadLooksStalled } from "./jobEvents";

const job = (over: Partial<Job> = {}): Job => ({
  id: 1,
  kind: "image_local",
  status: "running",
  progress: 0.2,
  message: "",
  params: { prompt: "keep me" },
  result: {},
  error: "",
  created_at: "",
  started_at: null,
  finished_at: null,
  ...over,
});

describe("applyJobEvent", () => {
  it("ignores non-job events and events without an id", () => {
    expect(applyJobEvent({}, { type: "ping" })).toBeNull();
    expect(applyJobEvent({}, { type: "job" })).toBeNull();
  });

  it("inserts a new job from a WS event", () => {
    const r = applyJobEvent({}, { type: "job", id: 7, status: "queued", kind: "t2v" })!;
    expect(r.jobs[7].status).toBe("queued");
    expect(r.jobs[7].kind).toBe("t2v");
  });

  it("merges partial events and preserves existing params", () => {
    const r = applyJobEvent({ 1: job() }, { type: "job", id: 1, progress: 0.5 })!;
    expect(r.jobs[1].progress).toBe(0.5);
    expect(r.jobs[1].params.prompt).toBe("keep me");
  });

  it("does not leak websocket envelope fields into the job domain row", () => {
    const r = applyJobEvent(
      { 1: job() },
      { type: "job", id: 1, progress: 0.6, lane: "local", future_envelope: "x" },
    )!;
    expect(r.jobs[1]).not.toHaveProperty("type");
    expect(r.jobs[1]).not.toHaveProperty("lane");
    expect(r.jobs[1]).not.toHaveProperty("future_envelope");
  });

  it("accepts job_id as the id key", () => {
    const r = applyJobEvent({}, { type: "job", job_id: 9, status: "running" })!;
    expect(r.jobs[9].id).toBe(9);
  });

  it("flags preview cleanup when a job finishes", () => {
    // Previews themselves arrive as binary frames now (lib/wsframe.ts); this
    // stream only says when to drop one and revoke its object URL.
    const r = applyJobEvent({ 1: job() }, { type: "job", id: 1, status: "done" })!;
    expect(r.clearPreview).toBe(1);
  });

  it("does not flag cleanup while a job is still running", () => {
    const r = applyJobEvent({ 1: job() }, { type: "job", id: 1, progress: 0.4 })!;
    expect(r.clearPreview).toBeUndefined();
  });
});

describe("model load events", () => {
  it("records a stage without touching job progress", () => {
    // The point: a load has no meaningful fraction. Moving the bar for it would
    // make a 90-second stall look like generation progress.
    const r = applyJobEvent(
      { 1: job({ progress: 0.4 }) },
      {
        type: "model_load",
        id: 1,
        kind: "image_local",
        stage: "downloading",
        detail: "8.4 GB",
        beat: false,
      },
    )!;
    expect(r.load?.state?.stage).toBe("downloading");
    expect(r.load?.state?.detail).toBe("8.4 GB");
    expect(r.jobs[1].progress).toBe(0.4);
  });

  it("clears the load once the model is ready", () => {
    const r = applyJobEvent(
      { 1: job() },
      {
        type: "model_load",
        id: 1,
        kind: "image_local",
        stage: "ready",
        detail: "loaded",
        beat: false,
      },
    )!;
    expect(r.load).toEqual({ id: 1, state: null });
  });

  it("clears the load when the job ends, even mid-load", () => {
    // A job that fails while loading must not leave a spinner behind forever.
    const r = applyJobEvent({ 1: job() }, { type: "job", id: 1, status: "error", error: "boom" })!;
    expect(r.load).toEqual({ id: 1, state: null });
  });

  it("carries a failure tip alongside the traceback", () => {
    const r = applyJobEvent(
      { 1: job() },
      {
        type: "job",
        id: 1,
        status: "error",
        error: "CUDA out of memory\n  at ...",
        tip: "The GPU ran out of memory. Try a lower quality tier.",
      },
    )!;
    expect(r.jobs[1].tip).toContain("lower quality tier");
    expect(r.jobs[1].error).toContain("CUDA out of memory");
  });

  it("names every stage the backend can send", () => {
    for (const s of [
      "swapping",
      "resolving",
      "downloading",
      "quantizing",
      "loading",
      "placing",
      "ready",
    ] as const) {
      expect(LOAD_STAGE_LABEL[s]).toBeTruthy();
    }
  });
});

describe("load liveness", () => {
  it("treats a recent heartbeat as alive", () => {
    const now = Date.now();
    expect(loadLooksStalled({ stage: "downloading", detail: "", at: now }, now)).toBe(false);
  });

  it("flags a long silence as suspect", () => {
    // The heartbeat is every 2s, so 40s of silence is twenty missed beats.
    const now = Date.now();
    expect(loadLooksStalled({ stage: "downloading", detail: "", at: now - 40_000 }, now)).toBe(true);
  });

  it("says nothing about a job that is not loading", () => {
    expect(loadLooksStalled(null)).toBe(false);
    expect(loadLooksStalled(undefined)).toBe(false);
  });
});
