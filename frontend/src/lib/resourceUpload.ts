import { ApiError, api } from "../api/client";
import type { ResourceView, UploadSession } from "../api/types";

/** A network blip on one chunk is retried this many times before the upload fails. */
const CHUNK_TRIES = 4;

export interface UploadProgress {
  sent: number;
  size: number;
  phase: "uploading" | "verifying" | "importing";
}

/**
 * Upload a local file as a resource through the chunked receipts.
 *
 * Each chunk stays far below the server's request cap. When the server holds a
 * different offset (a chunk landed but its answer was lost), the refusal says
 * where it is and the upload resumes there instead of starting over. Aborting
 * cancels the server session too, so no partial file is left behind.
 */
export async function uploadResource(
  file: File,
  kind: "adapter" | "model",
  options: { label?: string; signal?: AbortSignal; onProgress?: (p: UploadProgress) => void } = {},
): Promise<{ status: "imported" | "already_present"; resource: ResourceView }> {
  const { signal, onProgress } = options;
  let session: UploadSession = await api.createUpload({ filename: file.name, size: file.size, kind });
  try {
    let offset = session.received;
    let failures = 0;
    while (offset < file.size) {
      if (signal?.aborted) throw new DOMException("Upload canceled", "AbortError");
      onProgress?.({ sent: offset, size: file.size, phase: "uploading" });
      const chunk = file.slice(offset, Math.min(file.size, offset + session.chunk_bytes));
      try {
        session = await api.uploadChunk(session.upload_id, offset, chunk, signal);
        offset = session.received;
        failures = 0;
      } catch (error) {
        if (signal?.aborted) throw error;
        const held = error instanceof ApiError ? error.state?.received : undefined;
        const resumable =
          error instanceof ApiError && error.code === "offset_mismatch" && typeof held === "number";
        const retryable =
          resumable || !(error instanceof ApiError) || error.retryable === true || error.status >= 500;
        // A server that keeps disagreeing counts as failing, so this cannot loop forever.
        if (!retryable || ++failures >= CHUNK_TRIES) throw error;
        if (resumable) offset = held;
      }
    }
    onProgress?.({ sent: file.size, size: file.size, phase: "verifying" });
    session = await api.completeUpload(session.upload_id);
    onProgress?.({ sent: file.size, size: file.size, phase: "importing" });
    return await api.importUpload(session.upload_id, options.label ?? "");
  } catch (error) {
    if (session.state !== "complete") await api.cancelUpload(session.upload_id).catch(() => {});
    throw error;
  }
}
