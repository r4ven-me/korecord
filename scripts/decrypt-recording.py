#!/usr/bin/env python3
"""Standalone decryption for korecord's encrypted recordings.

Deliberately does NOT import or require korecord itself -- if korecord is
broken, gone, or you're on a machine that never had it installed, this is
still enough to get your data back. Needs only the `cryptography` package
(`pip install cryptography`) -- everything else (tar, zstd) is handled by
the standard library / a bundled decompressor call.

A session is a single `.rec` file: a plain (uncompressed) tar archive
holding a "cast" member (the asciicast recording) and, once korecord's
background rendering finished, a "txt" member (the searchable plaintext
transcript). This script extracts whichever members are present.

Each member is independently:

  member = salt (16 bytes) || nonce (12 bytes) || AES-256-GCM(key, nonce, payload)
  key    = scrypt(password, salt, n=2**14, r=8, p=1, dklen=32)
  payload = zstd_compressed_data, or the raw bytes, depending on whether
            compression was on for that session -- detected here from the
            zstd frame's magic number, since (unlike the old per-file
            ".zst"/".enc" suffix scheme) that's no longer in the filename.

The salt is generated fresh per member and embedded directly in it -- not
secret, and nothing else to look up -- so the password (whatever you set
with `korec config encryption enable`, or a session-specific one set via
`korec encrypt`) is always enough on its own.

Usage:
    decrypt-recording.py <session.rec> [output-prefix]

Reads the password from $KORECORD_PASSWORD if set, otherwise prompts.
Writes <output-prefix>.cast and, if present, <output-prefix>.txt
(output-prefix defaults to the input filename with ".rec" stripped).
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
except ImportError:
    sys.exit("This script needs the 'cryptography' package: pip install cryptography")

SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32
HEADER_SIZE = SALT_SIZE + NONCE_SIZE
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_SIZE, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


def decrypt_member(blob: bytes, password: str, label: str) -> bytes:
    if len(blob) < HEADER_SIZE:
        sys.exit(f"{label} is too short to be a valid encrypted member")
    salt = blob[:SALT_SIZE]
    nonce = blob[SALT_SIZE:HEADER_SIZE]
    ciphertext = blob[HEADER_SIZE:]
    key = derive_key(password, salt)
    try:
        payload = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as e:
        detail = f" ({e})" if str(e) else ""
        sys.exit(
            f"Decryption of {label} failed -- wrong password, the file is corrupted/tampered "
            f"with, or it was never encrypted in the first place{detail}"
        )

    if payload[:4] == ZSTD_MAGIC:
        proc = subprocess.run(["zstd", "-d", "-c"], input=payload, capture_output=True)
        if proc.returncode != 0:
            sys.exit(f"zstd decompression of {label} failed:\n{proc.stderr.decode(errors='replace')}")
        return proc.stdout
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("rec_file", type=Path, help="A session's .rec file")
    parser.add_argument(
        "output_prefix", type=Path, nargs="?",
        help="Defaults to the input filename with .rec stripped -- writes <prefix>.cast/.txt",
    )
    args = parser.parse_args()

    try:
        tar = tarfile.open(args.rec_file, "r")
    except tarfile.TarError as e:
        sys.exit(f"{args.rec_file} isn't a valid .rec archive: {e}")

    with tar:
        members = {m.name: m for m in tar.getmembers() if m.name in ("cast", "txt")}
        if not members:
            sys.exit(f"{args.rec_file} has no 'cast' or 'txt' member -- is this really a korecord .rec file?")

        password = os.environ.get("KORECORD_PASSWORD")
        if not password:
            password = getpass.getpass("Password: ")

        prefix = args.output_prefix or args.rec_file.with_suffix("")
        for name, member in members.items():
            blob = tar.extractfile(member).read()
            output = Path(f"{prefix}.{name}")
            output.write_bytes(decrypt_member(blob, password, f"{args.rec_file}:{name}"))
            print(f"Decrypted {args.rec_file}:{name} -> {output}")


if __name__ == "__main__":
    main()
