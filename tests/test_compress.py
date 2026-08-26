from __future__ import annotations

import pytest

from korecord import crypto
from korecord.compress import compress_bytes_to_file, decompress_file, decrypt_file_with_retry


def test_bytes_roundtrip(tmp_path):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"some text", dest)
    assert decompress_file(dest) == b"some text"


def test_empty_bytes_roundtrip(tmp_path):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"", dest)
    assert decompress_file(dest) == b""


def test_creates_parent_dirs(tmp_path):
    dest = tmp_path / "nested" / "deeper" / "out.zst"
    compress_bytes_to_file(b"x", dest)
    assert dest.exists()
    assert decompress_file(dest) == b"x"


def test_encrypted_roundtrip(tmp_path):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"some secret text" * 100, dest, password="hunter2")
    assert decompress_file(dest, password="hunter2") == b"some secret text" * 100


def test_encrypted_file_is_not_plain_zstd_on_disk(tmp_path):
    """The whole point -- what's on disk must not be readable as ordinary
    zstd without the password."""
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"some secret text", dest, password="hunter2")
    with pytest.raises(Exception):
        decompress_file(dest)  # no password -- must not silently succeed


def test_encrypted_file_wrong_password_fails_cleanly(tmp_path):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"some secret text", dest, password="hunter2")
    with pytest.raises(crypto.DecryptionError):
        decompress_file(dest, password="wrong")


def test_unencrypted_file_read_without_password_still_works(tmp_path):
    """Plain (password=None) files must keep working exactly as before --
    encryption is opt-in per file, not a format change for everyone."""
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"plain text", dest)
    assert decompress_file(dest, password=None) == b"plain text"


def test_two_files_encrypted_with_the_same_password_get_different_salts(tmp_path):
    """Each file is self-contained (crypto.py embeds a fresh salt per
    call) -- encrypting two different files with the same password must
    not produce byte-identical headers."""
    a, b = tmp_path / "a.zst", tmp_path / "b.zst"
    compress_bytes_to_file(b"same content", a, password="hunter2")
    compress_bytes_to_file(b"same content", b, password="hunter2")
    assert a.read_bytes() != b.read_bytes()
    assert decompress_file(a, password="hunter2") == decompress_file(b, password="hunter2") == b"same content"


# --- decrypt_file_with_retry --------------------------------------------

def test_decrypt_file_with_retry_succeeds_on_first_try(tmp_path):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"secret", dest, password="hunter2")
    assert decrypt_file_with_retry(dest, "hunter2", "session 1") == b"secret"


def test_decrypt_file_with_retry_prompts_when_no_password_given(tmp_path, monkeypatch):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"secret", dest, password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hunter2")
    assert decrypt_file_with_retry(dest, None, "session 1") == b"secret"


def test_decrypt_file_with_retry_retries_a_different_password(tmp_path, monkeypatch):
    """The key finding this test locks in: sessions can each have their
    own password (every file's salt is embedded independently -- see
    crypto.py) -- a password that's wrong for THIS file shouldn't be a
    dead end, just a prompt for a different one."""
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"secret", dest, password="the-actual-password")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "the-actual-password")
    # started with a wrong password already in hand -- must recover via
    # the interactive retry rather than giving up immediately
    assert decrypt_file_with_retry(dest, "wrong-password", "session 1") == b"secret"


def test_decrypt_file_with_retry_gives_up_after_max_attempts(tmp_path, monkeypatch):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"secret", dest, password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "still-wrong")
    assert decrypt_file_with_retry(dest, "wrong", "session 1", max_attempts=2) is None


def test_decrypt_file_with_retry_returns_none_without_tty_and_no_password(tmp_path, monkeypatch):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"secret", dest, password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert decrypt_file_with_retry(dest, None, "session 1") is None


def test_decrypt_file_with_retry_wrong_password_no_tty_returns_none_without_hanging(tmp_path, monkeypatch):
    dest = tmp_path / "out.zst"
    compress_bytes_to_file(b"secret", dest, password="hunter2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert decrypt_file_with_retry(dest, "wrong", "session 1") is None
