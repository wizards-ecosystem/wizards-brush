import { describe, expect, it, vi } from "vitest";

// Mock the API/socket layer so the store can be driven without a backend.
const mocks = vi.hoisted(() => ({
  handler: { fn: (() => {}) as (e: unknown) => void },
  frames: { fn: (() => {}) as (f: unknown) => void },
  api: {
    models: () => Promise.resolve([{ id: "x", title: "X", output: "image" }]),
    presets: () => Promise.resolve({}),
    system: () => Promise.resolve({}),
    jobs: () => Promise.resolve([] as unknown[]),
    assets: () => Promise.resolve({ items: [], total: 0 }),
    stats: () => Promise.resolve({}),
  },
}));

vi.mock("../api/client", () => ({
  api: mocks.api,
  jobSocket: (onEvent: (e: unknown) => void, onFrame?: (f: unknown) => void) => {
    mocks.handler.fn = onEvent; // capture the store's event handler to drive it
    if (onFrame) mocks.frames.fn = onFrame;
    return { close() {}, onclose: null, onopen: null } as unknown as WebSocket;
  },
}));

// Object URLs do not exist in jsdom. Stub them so the store's create/revoke
// bookkeeping can be observed, which is the part that leaks if it is wrong.
const revoked: string[] = [];
let urlCounter = 0;
globalThis.URL.createObjectURL = () => `blob:preview-${++urlCounter}`;
globalThis.URL.revokeObjectURL = (u: string) => {
  revoked.push(u);
};

import { useStore } from "./useStore";

const emit = (e: Record<string, unknown>) => mocks.handler.fn(e);
/** Deliver a preview as the binary frame the server actually sends. */
const emitPreview = (jobId: number) =>
  mocks.frames.fn({ type: 1, jobId, payload: new Blob([new Uint8Array([1, 2])]) });

describe("useStore", () => {
  it("boot loads specs and marks itself booted", async () => {
    await useStore.getState().boot();
    expect(useStore.getState().specs).toHaveLength(1);
    expect(useStore.getState().booted).toBe(true);
  });

  it("keeps previews out of the jobs map, in the previews map", async () => {
    await useStore.getState().boot();
    emit({ type: "job", id: 7, kind: "image_local", status: "running", progress: 0.5 });
    emitPreview(7);
    const s = useStore.getState();
    expect(s.jobs[7].status).toBe("running");
    expect(s.previews[7]).toMatch(/^blob:/);
    expect((s.jobs[7] as unknown as Record<string, unknown>).preview).toBeUndefined();
  });

  it("clears a job's preview on a terminal event and revokes its object URL", async () => {
    // Object URLs are never garbage collected. One arrives several times a
    // second, so failing to revoke leaks steadily for the life of the tab.
    await useStore.getState().boot();
    emit({ type: "job", id: 8, status: "running", progress: 0.5 });
    emitPreview(8);
    const url = useStore.getState().previews[8];
    expect(url).toBeDefined();
    emit({ type: "job", id: 8, status: "done" });
    expect(useStore.getState().previews[8]).toBeUndefined();
    expect(revoked).toContain(url);
  });

  it("revokes the previous URL when a preview is replaced", async () => {
    await useStore.getState().boot();
    emit({ type: "job", id: 11, status: "running", progress: 0.1 });
    emitPreview(11);
    const first = useStore.getState().previews[11];
    emitPreview(11);
    expect(useStore.getState().previews[11]).not.toBe(first);
    expect(revoked).toContain(first);
  });

  it("refreshJobs prunes previews for jobs no longer running", async () => {
    await useStore.getState().boot();
    useStore.setState({ previews: { 9: "blob:stale-preview" } });
    mocks.api.jobs = () => Promise.resolve([{ id: 9, status: "done" }]);
    await useStore.getState().refreshJobs();
    expect(useStore.getState().previews[9]).toBeUndefined();
    expect(revoked).toContain("blob:stale-preview");
  });
});
