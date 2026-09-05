"""The binary WebSocket frame format.

A preview is a JPEG. Base64 inside a JSON event costs 33% plus escaping plus a
parse, on the hottest event in the system.
"""
from __future__ import annotations

import struct

from backend.app import wsframe
from backend.app.wsframe import FrameType, decode, encode


def test_round_trip():
    payload = b"\xff\xd8\xff\xe0 not really a jpeg"
    got = decode(encode(FrameType.preview_jpeg, 42, payload))
    assert got == (FrameType.preview_jpeg, 42, payload)


def test_the_header_is_big_endian_so_both_sides_agree_without_a_flag():
    """struct.pack('>I') and DataView.getUint32() agree by default only in
    big-endian, which removes a class of mistake at the boundary."""
    frame = encode(FrameType.preview_jpeg, 0x01020304, b"")
    assert frame[:4] == b"\x00\x00\x00\x01", "frame type"
    assert frame[4:8] == b"\x01\x02\x03\x04", "job id, big-endian"


def test_an_empty_payload_is_valid():
    assert decode(encode(FrameType.preview_jpeg, 1, b"")) == (FrameType.preview_jpeg, 1, b"")


def test_a_short_message_is_ignored_not_an_error():
    assert decode(b"") is None
    assert decode(b"\x00\x00\x00\x01") is None


def test_an_unknown_frame_type_is_ignored():
    """A client and server on different versions is ordinary, not a protocol
    error — an unrecognised frame should be skipped, not raised on."""
    assert decode(struct.pack(">II", 9999, 1) + b"data") is None


def test_frame_type_numbers_are_stable():
    """These are on the wire. Changing one breaks every connected client."""
    assert FrameType.preview_jpeg == 1


def test_large_job_ids_survive():
    got = decode(encode(FrameType.preview_jpeg, 4_000_000_000, b"x"))
    assert got is not None and got[1] == 4_000_000_000


def test_the_hub_marks_binary_frames_droppable():
    """A client behind a flood should skip previews, never a state transition."""
    from backend.app.queue import ProgressHub

    hub = ProgressHub()
    q = hub.subscribe()
    try:
        for _ in range(q.maxsize + 10):
            hub.emit_binary(wsframe.encode(FrameType.preview_jpeg, 1, b"x"))
        hub.emit({"type": "job", "id": 1, "status": "done"})
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        terminal = [e for e in drained if e.get("status") == "done"]
        assert terminal, "the terminal event must survive a flood of previews"
    finally:
        hub.unsubscribe(q)
