"""Reading a file a worker could have planted (review round 1, blocker 3).

The path must lie inside `root`, no component between `root` and the file
may be a symlink, the file itself is opened with O_NOFOLLOW and must be a
regular file, and its size is capped. Anything else is refused, and callers
treat a refusal as a missing artifact (which fails closed)."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

DEFAULT_MAX = 1 << 20  # 1 MiB


class UnsafeFile(OSError):
    pass


def safe_read_bytes(root: Path, path: Path, max_bytes: int = DEFAULT_MAX) -> bytes:
    root_real = Path(os.path.realpath(root))
    try:
        rel = Path(os.path.abspath(path)).relative_to(os.path.abspath(root))
    except ValueError as e:
        raise UnsafeFile(f"{path} is outside {root}") from e
    if not rel.parts or ".." in rel.parts:
        raise UnsafeFile(f"{path}: bad path")
    cur = root_real
    for part in rel.parts[:-1]:
        cur = cur / part
        st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise UnsafeFile(f"{cur} is not a plain directory")
    try:
        # O_NONBLOCK: opening a FIFO doesn't wait for a writer (review round 2, N3);
        # fstat below refuses anything but a regular file before any read.
        fd = os.open(cur / rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise UnsafeFile(f"{path} is a symlink") from e
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFile(f"{path} is not a regular file")
        if st.st_size > max_bytes:
            raise UnsafeFile(f"{path} is {st.st_size} bytes; the cap is {max_bytes}")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            b = os.read(fd, min(remaining, 1 << 16))
            if not b:
                break
            chunks.append(b)
            remaining -= len(b)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise UnsafeFile(f"{path} grew past the cap while reading")
        return data
    finally:
        os.close(fd)


def safe_read_text(root: Path, path: Path, max_bytes: int = DEFAULT_MAX) -> str:
    return safe_read_bytes(root, path, max_bytes).decode("utf-8", errors="replace")
