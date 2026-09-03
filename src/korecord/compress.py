"""zstd compression + optional AES-GCM encryption, applied to raw bytes
rather than whole files -- korecord stores a session's cast/txt content as
members inside one .rec tar archive (see archive.py), so packing/
unpacking happens on one member's bytes at a time, not a standalone file.

Compression is on by default but can be turned off (see config.py's
compression_enabled/set_compression) for raw throughput on a very
high-output session where the zstd pass itself becomes the bottleneck.
Compression and encryption are independent -- either, both, or neither can
apply to a given member; that's why both are unpacked here for a
member's own `compressed`/`encrypted` flags (see db.py) rather than
inferred from anything about the bytes themselves.

See crypto.py for the cipher/KDF and the embedded-salt file format."""
from __future__ import annotations

import getpass
import io
import sys

import zstandard as zstd

from . import crypto


def pack_bytes(data: bytes, *, compress: bool = True, password: str | None = None, level: int = 19) -> bytes:
    out = zstd.ZstdCompressor(level=level, threads=-1).compress(data) if compress else data
    if password is not None:
        out = crypto.encrypt(out, password)
    return out


def unpack_bytes(blob: bytes, *, compressed: bool = True, password: str | None = None) -> bytes:
    if password is not None:
        blob = crypto.decrypt(blob, password)
    if compressed:
        return zstd.ZstdDecompressor().stream_reader(io.BytesIO(blob)).read()
    return blob


def unpack_bytes_with_retry(
    blob: bytes, *, compressed: bool, password: str | None, label: str, max_attempts: int = 3
) -> bytes | None:
    """Like unpack_bytes, but for an encrypted member: retries with a
    freshly-prompted password if the one already in hand doesn't open it
    -- each member's salt is embedded in it (see crypto.py), so sessions
    aren't forced to share one password; a wrong password for *this*
    session doesn't mean anything is actually broken, just that a
    different one applies here.

    `label` identifies what's being decrypted in the prompt (e.g.
    "session 42"). Returns None (after printing why) if `password` was
    never given and there's no tty to prompt on, or retries run out --
    never raises for an ordinary wrong-password case."""
    attempt = 0
    while True:
        if password is not None:
            try:
                return unpack_bytes(blob, compressed=compressed, password=password)
            except crypto.DecryptionError:
                print(f"korec: that password didn't decrypt {label}", file=sys.stderr)
        if attempt >= max_attempts or not sys.stdin.isatty():
            return None
        prompt = (
            f"korec: password for {label}: " if password is None else
            f"korec: try a different password for {label}: "
        )
        password = getpass.getpass(prompt)
        attempt += 1
