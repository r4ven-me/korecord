from __future__ import annotations

import pytest

from korecord import crypto
from korecord.compress import pack_bytes, unpack_bytes, unpack_bytes_with_retry


def test_bytes_roundtrip():
    blob = pack_bytes(b"some text")
    assert unpack_bytes(blob) == b"some text"


def test_empty_bytes_roundtrip():
    blob = pack_bytes(b"")
    assert unpack_bytes(blob) == b""


def test_uncompressed_roundtrip():
    """compress=False skips zstd entirely -- the packed bytes are the raw
    input, not a zstd frame."""
    blob = pack_bytes(b"some text", compress=False)
    assert blob == b"some text"
    assert unpack_bytes(blob, compressed=False) == b"some text"


def test_encrypted_roundtrip():
    blob = pack_bytes(b"some secret text" * 100, password="hunter2")
    assert unpack_bytes(blob, password="hunter2") == b"some secret text" * 100


def test_encrypted_and_uncompressed_together():
    blob = pack_bytes(b"secret", compress=False, password="hunter2")
    assert unpack_bytes(blob, compressed=False, password="hunter2") == b"secret"


def test_encrypted_bytes_are_not_plain_zstd():
    """The whole point -- what's packed must not be readable as ordinary
    zstd without the password."""
    blob = pack_bytes(b"some secret text", password="hunter2")
    with pytest.raises(Exception):
        unpack_bytes(blob)  # no password -- must not silently succeed


def test_encrypted_wrong_password_fails_cleanly():
    blob = pack_bytes(b"some secret text", password="hunter2")
    with pytest.raises(crypto.DecryptionError):
        unpack_bytes(blob, password="wrong")


def test_unencrypted_bytes_read_without_password_still_works():
    """Plain (password=None) content must keep working exactly as before
    -- encryption is opt-in per member, not a format change for everyone."""
    blob = pack_bytes(b"plain text")
    assert unpack_bytes(blob, password=None) == b"plain text"


def test_two_packs_with_the_same_password_get_different_salts():
    """Each pack is self-contained (crypto.py embeds a fresh salt per
    call) -- packing the same content twice with the same password must
    not produce byte-identical output."""
    a = pack_bytes(b"same content", password="hunter2")
    b = pack_bytes(b"same content", password="hunter2")
    assert a != b
    assert unpack_bytes(a, password="hunter2") == unpack_bytes(b, password="hunter2") == b"same content"


# --- unpack_bytes_with_retry ------------------------------------------------

def test_unpack_bytes_with_retry_succeeds_on_first_try():
    blob = pack_bytes(b"secret", password="hunter2")
    assert unpack_bytes_with_retry(blob, compressed=True, password="hunter2", label="session 1") == b"secret"


def test_unpack_bytes_with_retry_prompts_when_no_password_given(monkeypatch):
    blob = pack_bytes(b"secret", password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hunter2")
    assert unpack_bytes_with_retry(blob, compressed=True, password=None, label="session 1") == b"secret"


def test_unpack_bytes_with_retry_retries_a_different_password(monkeypatch):
    """The key finding this test locks in: sessions can each have their
    own password (every member's salt is embedded independently -- see
    crypto.py) -- a password that's wrong for THIS member shouldn't be a
    dead end, just a prompt for a different one."""
    blob = pack_bytes(b"secret", password="the-actual-password")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "the-actual-password")
    # started with a wrong password already in hand -- must recover via
    # the interactive retry rather than giving up immediately
    assert unpack_bytes_with_retry(blob, compressed=True, password="wrong-password", label="session 1") == b"secret"


def test_unpack_bytes_with_retry_gives_up_after_max_attempts(monkeypatch):
    blob = pack_bytes(b"secret", password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "still-wrong")
    assert unpack_bytes_with_retry(blob, compressed=True, password="wrong", label="session 1", max_attempts=2) is None


def test_unpack_bytes_with_retry_returns_none_without_tty_and_no_password(monkeypatch):
    blob = pack_bytes(b"secret", password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert unpack_bytes_with_retry(blob, compressed=True, password=None, label="session 1") is None


def test_unpack_bytes_with_retry_wrong_password_no_tty_returns_none_without_hanging(monkeypatch):
    blob = pack_bytes(b"secret", password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert unpack_bytes_with_retry(blob, compressed=True, password="wrong", label="session 1") is None
