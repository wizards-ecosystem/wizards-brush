import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import type { UploadSession } from "../api/types";
import { uploadResource } from "./resourceUpload";

const session = (over: Partial<UploadSession> = {}): UploadSession => ({
  upload_id: "u1",
  filename: "a.safetensors",
  kind: "adapter",
  size: 10,
  received: 0,
  state: "open",
  sha256: null,
  chunk_bytes: 4,
  ...over,
});

afterEach(() => vi.restoreAllMocks());

const file = () => new File([new Uint8Array(10)], "a.safetensors");

describe("uploadResource", () => {
  it("sends chunks no larger than the server asks, then completes and imports the receipt", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue(session());
    const chunk = vi
      .spyOn(api, "uploadChunk")
      .mockImplementation(async (_id, offset, blob) => session({ received: offset + blob.size }));
    vi.spyOn(api, "completeUpload").mockResolvedValue(session({ received: 10, state: "complete" }));
    const imported = vi
      .spyOn(api, "importUpload")
      .mockResolvedValue({ status: "imported", resource: { label: "a" } as never });
    const phases: string[] = [];
    await uploadResource(file(), "adapter", { label: "mine", onProgress: (p) => phases.push(p.phase) });
    expect(chunk.mock.calls.map(([, offset, blob]) => [offset, blob.size])).toEqual([
      [0, 4],
      [4, 4],
      [8, 2],
    ]);
    expect(imported).toHaveBeenCalledWith("u1", "mine");
    expect(phases.slice(-2)).toEqual(["verifying", "importing"]);
  });

  it("resumes where the server says when a chunk's answer was lost", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue(session());
    const chunk = vi
      .spyOn(api, "uploadChunk")
      .mockResolvedValueOnce(session({ received: 4 }))
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(
        new ApiError(409, { code: "offset_mismatch", retryable: true, state: { received: 8 } }, "expected 8"),
      )
      .mockResolvedValueOnce(session({ received: 10 }));
    vi.spyOn(api, "completeUpload").mockResolvedValue(session({ state: "complete" }));
    vi.spyOn(api, "importUpload").mockResolvedValue({ status: "imported", resource: {} as never });
    await uploadResource(file(), "adapter");
    expect(chunk.mock.calls.map(([, offset]) => offset)).toEqual([0, 4, 4, 8]);
  });

  it("cancels the server session when aborted or refused, leaving no partial file", async () => {
    vi.spyOn(api, "createUpload").mockResolvedValue(session());
    vi.spyOn(api, "uploadChunk").mockRejectedValue(new ApiError(400, { code: "too_large" }, "too large"));
    const cancel = vi.spyOn(api, "cancelUpload").mockResolvedValue({ ok: true } as never);
    await expect(uploadResource(file(), "adapter")).rejects.toThrow("too large");
    expect(cancel).toHaveBeenCalledWith("u1");

    const abort = new AbortController();
    abort.abort();
    await expect(uploadResource(file(), "adapter", { signal: abort.signal })).rejects.toThrow(/canceled/);
    expect(cancel).toHaveBeenCalledTimes(2);
  });
});
