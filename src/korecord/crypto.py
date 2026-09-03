"""Optional password-based encryption for recordings.

Layered *on top of* compression, not instead of it: compress first with
zstd, then encrypt the (incompressible, high-entropy) result -- encrypting
first would make the zstd step pointless. Off by default; see `korec config
encryption`.

Threat model this defends against: someone getting hold of a *copy* of the
data directory separately from the config file -- a backup, a synced
folder, an old disk -- not someone with live shell access to this machine
while both files sit under the same $HOME. That's *why* the config file is
allowed to hold the password at all: convenience is worth it against that
specific threat, not against a local attacker who already has filesystem
access to both.

Each file carries its own randomly-generated salt, embedded directly in
it (`salt || nonce || ciphertext`) -- so the password *alone* is always
enough to decrypt any given file, with nothing extra to keep track of or
lose, and no external state (a salt kept elsewhere, say) that could ever
go missing and orphan already-encrypted data. It doesn't need to be
secret -- only the password does -- so there's no security cost to this,
and each file being fully self-contained also means different sessions
are free to use different passwords, and korec can just ask again if the
one it has on hand doesn't open a particular one.

Actual encryption is AES-256-GCM with a fresh random nonce per file, which
also means tampering or corruption is *detected* (authentication failure)
rather than silently producing garbage. The key itself is derived from the
password via scrypt, deliberately slow/memory-hard to resist brute-forcing
-- which does mean every file pays that cost independently (no shared key
to reuse across a `korec grep` touching many encrypted sessions), a
deliberate trade of a bit of speed for not depending on any external state
surviving.
"""
from __future__ import annotations

import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32  # AES-256
HEADER_SIZE = SALT_SIZE + NONCE_SIZE

# scrypt cost parameters. n=2**14 with r=8, p=1 is the library's own
# "interactive" recommendation -- on the order of ~100-300ms on ordinary
# hardware, slow enough to matter for brute-forcing, fast enough not to be
# a real bottleneck even paid once per file.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class DecryptionError(Exception):
    """Wrong password, or the file is corrupted/tampered with -- AES-GCM's
    authentication tag didn't verify. Deliberately distinct from a bare
    zstd/JSON decode failure so callers can give a more useful message."""


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_SIZE)


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_SIZE, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


def encrypt(data: bytes, password: str) -> bytes:
    """Returns `salt || nonce || ciphertext` -- neither the salt nor the
    nonce need to be secret, just unique per encryption, so both travel
    alongside the data rather than needing separate storage. A fresh
    random salt every call, so decrypting only ever needs the password."""
    salt = new_salt()
    key = derive_key(password, salt)
    nonce = os.urandom(NONCE_SIZE)
    return salt + nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt(blob: bytes, password: str) -> bytes:
    if len(blob) < HEADER_SIZE:
        raise DecryptionError("encrypted data is truncated (shorter than a salt+nonce)")
    salt = blob[:SALT_SIZE]
    nonce = blob[SALT_SIZE:HEADER_SIZE]
    ciphertext = blob[HEADER_SIZE:]
    key = derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as e:
        raise DecryptionError("wrong password, or the file is corrupted/tampered with") from e
