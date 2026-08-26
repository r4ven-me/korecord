#!/usr/bin/env python3
"""Standalone decryption for korecord's encrypted recordings.

Deliberately does NOT import or require korecord itself -- if korecord is
broken, gone, or you're on a machine that never had it installed, this is
still enough to get your data back. Needs only:

  - the `cryptography` package (`pip install cryptography`)
  - a `zstd` binary on $PATH (near-universally available: apt/dnf/brew
    install zstd, or https://github.com/facebook/zstd/releases)

File format (see korecord's src/korecord/crypto.py for the canonical
version -- this script mirrors it exactly):

  file = salt (16 bytes) || nonce (12 bytes) || AES-256-GCM(key, nonce, zstd_compressed_data)
  key  = scrypt(password, salt, n=2**14, r=8, p=1, dklen=32)

The salt is generated fresh per file and embedded directly in it -- not
secret, and nothing else to look up -- so the password (whatever you set
with `korec config encryption enable`, or a session-specific one set via
`korec encrypt`) is always enough on its own.

Usage:
    decrypt-recording.py <file.cast.zst.enc|file.txt.zst.enc> [output-file]

Reads the password from $KORECORD_PASSWORD if set, otherwise prompts.
"""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
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


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_SIZE, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


def default_output_path(encrypted_file: Path) -> Path:
    name = encrypted_file.name
    if name.endswith(".enc"):
        name = name[: -len(".enc")]
    if name.endswith(".zst"):
        name = name[: -len(".zst")]
    return encrypted_file.parent / name


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("encrypted_file", type=Path, help="A *.cast.zst.enc or *.txt.zst.enc file")
    parser.add_argument(
        "output_file", type=Path, nargs="?",
        help="Defaults to the input filename with .enc and .zst stripped",
    )
    args = parser.parse_args()

    blob = args.encrypted_file.read_bytes()
    if len(blob) < HEADER_SIZE:
        sys.exit(f"{args.encrypted_file} is too short to be a valid encrypted recording")
    salt = blob[:SALT_SIZE]
    nonce = blob[SALT_SIZE:HEADER_SIZE]
    ciphertext = blob[HEADER_SIZE:]

    password = os.environ.get("KORECORD_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")
    key = derive_key(password, salt)

    try:
        zstd_data = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as e:
        detail = f" ({e})" if str(e) else ""
        sys.exit(f"Decryption failed -- wrong password, or the file is corrupted/tampered with{detail}")

    proc = subprocess.run(["zstd", "-d", "-c"], input=zstd_data, capture_output=True)
    if proc.returncode != 0:
        sys.exit(f"zstd decompression failed:\n{proc.stderr.decode(errors='replace')}")

    output = args.output_file or default_output_path(args.encrypted_file)
    output.write_bytes(proc.stdout)
    print(f"Decrypted {args.encrypted_file} -> {output}")


if __name__ == "__main__":
    main()
