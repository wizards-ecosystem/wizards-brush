"""Binary WebSocket frames, for payloads that are not text.

A live preview is a JPEG. Sending it as base64 inside a JSON event costs a 33%
size increase, JSON string escaping on the way out, and a parse plus a decode on
the way in — on the hottest event in the system, several times per generation
step, for a frame that is discarded 200 ms later.

A binary frame carries the bytes as bytes. The header says what it is:

    [4 bytes  big-endian uint32]  frame type
    [4 bytes  big-endian uint32]  job id
    [remaining bytes]             payload

Big-endian because that is what `struct.pack(">I", ...)` and JavaScript's
`DataView.getUint32()` agree on without an explicit endianness argument on either
side, which removes a class of mistake.

Adapted in design from ComfyUI's `BinaryEventTypes` (GPL-3.0 — reimplemented from
the wire format, which is not copyrightable, not from their code).
"""
from __future__ import annotations

import struct
from enum import IntEnum

HEADER = struct.Struct(">II")
HEADER_BYTES = HEADER.size


class FrameType(IntEnum):
    """What a binary frame contains. Append only — these numbers are on the wire."""

    preview_jpeg = 1


def encode(frame_type: FrameType, job_id: int, payload: bytes) -> bytes:
    """Header + payload, ready to send."""
    return HEADER.pack(int(frame_type), int(job_id)) + payload


def decode(message: bytes) -> tuple[FrameType, int, bytes] | None:
    """(type, job id, payload), or None if this is not a frame we understand.

    None rather than an exception: a client and server on different versions is
    an ordinary situation, and an unknown frame type should be ignored rather
    than treated as a protocol error.
    """
    if len(message) < HEADER_BYTES:
        return None
    raw_type, job_id = HEADER.unpack_from(message, 0)
    try:
        frame_type = FrameType(raw_type)
    except ValueError:
        return None
    return frame_type, job_id, message[HEADER_BYTES:]
