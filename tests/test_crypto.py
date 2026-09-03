from __future__ import annotations

import pytest

from korecord import crypto


def test_roundtrip():
    blob = crypto.encrypt(b"hello world", "hunter2")
    assert crypto.decrypt(blob, "hunter2") == b"hello world"


def test_roundtrip_empty_data():
    assert crypto.decrypt(crypto.encrypt(b"", "pw"), "pw") == b""


def test_wrong_password_fails_to_decrypt():
    blob = crypto.encrypt(b"secret data", "hunter2")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob, "wrong password")


def test_tampered_ciphertext_is_detected():
    blob = bytearray(crypto.encrypt(b"secret data", "pw"))
    blob[-1] ^= 1  # flip a bit in the auth tag / ciphertext tail
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(bytes(blob), "pw")


def test_tampered_salt_is_detected():
    """The salt is embedded in the file (see module docstring) -- flipping
    it changes the derived key just like a wrong password would, and
    should fail the same clean way, not crash."""
    blob = bytearray(crypto.encrypt(b"secret data", "pw"))
    blob[0] ^= 1  # flip a bit inside the embedded salt
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(bytes(blob), "pw")


def test_truncated_blob_raises_decryption_error_not_a_crash():
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(b"short", "pw")


def test_each_encryption_uses_a_fresh_salt_and_nonce():
    """Same password, same plaintext, twice -- ciphertexts must differ (a
    fresh random salt and nonce every call), even though both decrypt back
    to the same thing with just the password."""
    a = crypto.encrypt(b"same plaintext", "pw")
    b = crypto.encrypt(b"same plaintext", "pw")
    assert a != b
    assert a[: crypto.SALT_SIZE] != b[: crypto.SALT_SIZE]
    assert crypto.decrypt(a, "pw") == crypto.decrypt(b, "pw") == b"same plaintext"


def test_new_salt_is_random_and_correct_size():
    a, b = crypto.new_salt(), crypto.new_salt()
    assert a != b
    assert len(a) == crypto.SALT_SIZE == 16


def test_same_password_and_salt_derive_the_same_key():
    salt = crypto.new_salt()
    assert crypto.derive_key("hunter2", salt) == crypto.derive_key("hunter2", salt)


def test_different_salts_derive_different_keys():
    assert crypto.derive_key("hunter2", crypto.new_salt()) != crypto.derive_key("hunter2", crypto.new_salt())


def test_different_passwords_derive_different_keys():
    salt = crypto.new_salt()
    assert crypto.derive_key("hunter2", salt) != crypto.derive_key("hunter3", salt)


def test_decrypt_with_password_alone_needs_no_external_state():
    """The whole point: encrypt with just a password, and -- given only
    the resulting blob -- decrypt with just that same password again.
    Nothing else (no salt, no key) has to be kept around separately."""
    blob = crypto.encrypt(b"self-contained", "hunter2")
    # simulate "config.json was lost" -- nothing survives except the blob
    # itself and the password
    assert crypto.decrypt(blob, "hunter2") == b"self-contained"
