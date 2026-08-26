"""zstd helpers, in-process via python-zstandard -- no system `zstd` binary
required, so a pipx install of this package has everything it needs.

Optional encryption is layered on here too, rather than as a separate step
at every call site: compress-then-encrypt on write, decrypt-then-decompress
on read. See `crypto.py` for the actual cipher/KDF, the file format (each
file carries its own embedded salt -- the password alone is always enough
to decrypt), and the threat model it targets."""
from __future__ import annotations

import getpass
import io
import sys
from pathlib import Path

import zstandard as zstd

from . import crypto


def decompress_file(path: Path, password: str | None = None) -> bytes:
    raw = path.read_bytes()
    if password is not None:
        raw = crypto.decrypt(raw, password)
    dctx = zstd.ZstdDecompressor()
    return dctx.stream_reader(io.BytesIO(raw)).read()


def compress_bytes_to_file(data: bytes, dest_path: Path, level: int = 19, password: str | None = None) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    cctx = zstd.ZstdCompressor(level=level, threads=-1)
    out = cctx.compress(data)
    if password is not None:
        out = crypto.encrypt(out, password)
    dest_path.write_bytes(out)


def decrypt_file_with_retry(
    path: Path, password: str | None, label: str, *, max_attempts: int = 3
) -> bytes | None:
    """Decompress an encrypted file, retrying with a freshly-prompted
    password if the one already in hand doesn't open it -- each file's
    salt is embedded in the file itself (see crypto.py), so sessions
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
                return decompress_file(path, password=password)
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
