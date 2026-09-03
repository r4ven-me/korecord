"""A session's on-disk artifact: one tar container per session (the `.rec`
extension), holding two members -- "cast" (the asciicast recording) and,
once background rendering finishes, "txt" (the searchable transcript
sidecar). A session's format (compressed and/or encrypted) no longer shows
up in its filename -- every session is just `<base>.rec` regardless -- so
each member's bytes are packed by compress.py (zstd-compressed and/or
AES-GCM encrypted, per that session's own `compressed`/`encrypted` flags
in the index) before landing here as an opaque blob.

The tar container itself is never compressed, precisely so the "txt"
member can be *appended* later (see `append`) without touching the "cast"
member already written -- rendering a session's transcript can take
minutes (see README), and a session must stay playable via its "cast"
member the moment recording ends, not just once that background step
finishes too."""
from __future__ import annotations

import io
import tarfile
from pathlib import Path


def _write_member(path: Path, name: str, data: bytes, *, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, mode) as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def create(path: Path, name: str, data: bytes) -> None:
    """Creates `path` from scratch with one member -- overwrites anything
    already there."""
    _write_member(path, name, data, mode="w")


def append(path: Path, name: str, data: bytes) -> None:
    """Adds a member to an existing archive without touching what's
    already in it. `path` must already exist (see `create`)."""
    _write_member(path, name, data, mode="a")


def read_member(path: Path, name: str) -> bytes | None:
    """None if `path` doesn't have a member called `name` -- e.g. a
    finished session whose background transcript render hasn't caught up
    (or never ran) yet has no "txt" member."""
    with tarfile.open(path, "r") as tar:
        try:
            member = tar.getmember(name)
        except KeyError:
            return None
        f = tar.extractfile(member)
        return f.read() if f is not None else b""


def has_member(path: Path, name: str) -> bool:
    with tarfile.open(path, "r") as tar:
        return name in tar.getnames()
