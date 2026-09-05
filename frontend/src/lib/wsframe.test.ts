import { describe, expect, it } from "vitest";
import { FrameType, decodeFrame } from "./wsframe";

function frame(type: number, jobId: number, payload: Uint8Array): ArrayBuffer {
  const buf = new ArrayBuffer(8 + payload.byteLength);
  const view = new DataView(buf);
  view.setUint32(0, type);
  view.setUint32(4, jobId);
  new Uint8Array(buf, 8).set(payload);
  return buf;
}

describe("binary frames", () => {
  it("reads a preview frame", () => {
    const got = decodeFrame(frame(FrameType.previewJpeg, 42, new Uint8Array([1, 2, 3])));
    expect(got?.type).toBe(FrameType.previewJpeg);
    expect(got?.jobId).toBe(42);
    expect(got?.payload.size).toBe(3);
    expect(got?.payload.type).toBe("image/jpeg");
  });

  it("agrees with the backend on byte order", () => {
    // The backend writes struct.pack(">II", 1, 0x01020304). If either side
    // guessed endianness differently the job id would be nonsense.
    const buf = new Uint8Array([0, 0, 0, 1, 1, 2, 3, 4]).buffer;
    expect(decodeFrame(buf)?.jobId).toBe(0x01020304);
  });

  it("accepts an empty payload", () => {
    expect(decodeFrame(frame(FrameType.previewJpeg, 1, new Uint8Array()))?.payload.size).toBe(0);
  });

  it("ignores a truncated message rather than throwing", () => {
    expect(decodeFrame(new ArrayBuffer(0))).toBeNull();
    expect(decodeFrame(new ArrayBuffer(4))).toBeNull();
  });

  it("ignores an unknown frame type", () => {
    // A client and server on different versions is ordinary, not fatal.
    expect(decodeFrame(frame(9999, 1, new Uint8Array([1])))).toBeNull();
  });

  it("handles large job ids", () => {
    expect(decodeFrame(frame(FrameType.previewJpeg, 4_000_000_000, new Uint8Array()))?.jobId).toBe(
      4_000_000_000,
    );
  });
});
