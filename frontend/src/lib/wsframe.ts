/**
 * Binary WebSocket frames.
 *
 * A live preview is a JPEG. Carrying it as base64 inside a JSON event costs a
 * 33% size increase, string escaping, and a parse — several times per
 * generation step, for an image discarded 200 ms later. A binary frame carries
 * the bytes as bytes.
 *
 *   [4 bytes big-endian uint32]  frame type
 *   [4 bytes big-endian uint32]  job id
 *   [remaining]                  payload
 *
 * Big-endian because `DataView.getUint32()` and Python's `struct.pack(">I")`
 * agree there without an explicit endianness argument on either side.
 *
 * Must stay in step with `backend/app/wsframe.py`.
 */

export const FrameType = {
  previewJpeg: 1,
} as const;

export type FrameTypeValue = (typeof FrameType)[keyof typeof FrameType];

const HEADER_BYTES = 8;

export interface BinaryFrame {
  type: FrameTypeValue;
  jobId: number;
  payload: Blob;
}

const KNOWN: ReadonlySet<number> = new Set(Object.values(FrameType));

/**
 * Parse a binary message, or null if it is not one we understand.
 *
 * Null rather than throwing: a client and server on different versions is an
 * ordinary situation, and an unknown frame type should be skipped rather than
 * treated as a protocol error that kills the socket.
 */
export function decodeFrame(buffer: ArrayBuffer): BinaryFrame | null {
  if (buffer.byteLength < HEADER_BYTES) return null;
  const view = new DataView(buffer);
  const type = view.getUint32(0);
  if (!KNOWN.has(type)) return null;
  return {
    type: type as FrameTypeValue,
    jobId: view.getUint32(4),
    payload: new Blob([buffer.slice(HEADER_BYTES)], { type: "image/jpeg" }),
  };
}

/**
 * An object URL for a preview frame.
 *
 * Object URLs are not garbage collected — every one leaks until revoked, and a
 * preview arrives several times a second. **The caller owns the revoke**: pass
 * the URL this replaces to `URL.revokeObjectURL` before overwriting it, and
 * revoke the last one when the job reaches a terminal state. See useStore.
 */
export function previewObjectUrl(frame: BinaryFrame): string {
  return URL.createObjectURL(frame.payload);
}
