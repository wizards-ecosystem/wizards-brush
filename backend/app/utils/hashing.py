"""Content hashing for asset deduplication.

`hashlib.blake2b` rather than a third-party blake3: it is stdlib, it runs at
roughly a gigabyte a second, and this is a dedup identity rather than a security
boundary. Keeping it stdlib matters more than the speed difference — the project
ships as one self-contained checkout, and every added wheel is one more thing
that can fail to build on someone else's machine.

20 bytes (160 bits) of digest. Collision probability over a personal gallery is
so far below the probability of the disk lying to us that a longer hash would be
measuring the wrong risk, and the shorter hex is easier to read in a log line.

Two entry points. `write_and_hash` hashes *while* writing, in one pass over
bytes we already hold — hashing afterwards would mean reading the whole file
back off disk for no benefit. `hash_file` is for the other direction: rows
written before content addressing existed, whose files are already on disk.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

DIGEST_BYTES = 20
CHUNK = 1 << 20  # 1 MiB — large enough to amortize syscalls, small enough to stream


def _new():
    return hashlib.blake2b(digest_size=DIGEST_BYTES)


def hash_file(path: Path | str, *, chunk: int = CHUNK) -> str | None:
    """Hash a file already on disk. None if it cannot be read.

    Returns None rather than raising: this is called from the enrichment worker
    over rows whose files may have been moved or deleted, and a missing file is
    a state to record, not a crash.
    """
    h = _new()
    try:
        with Path(path).open("rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def write_and_hash(data: bytes, dest: Path, *, chunk: int = CHUNK) -> str:
    """Write `data` to `dest` and return its hash, in a single pass.

    The bytes are already in memory at every call site (a freshly encoded PNG, a
    video the remote lane just handed back), so hashing here costs one walk over
    memory we hold anyway. A 40 MB video adds roughly 20 ms.
    """
    h = _new()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            for i in range(0, len(data), chunk):
                block = data[i:i + chunk]
                h.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, dest)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return h.hexdigest()
