"""Runs scripts/decrypt-recording.py as a real subprocess against a file
encrypted through korecord's own crypto.py/compress.py -- guards against
the standalone script silently drifting out of sync with the actual file
format (embedded salt position/size, scrypt params, nonce size, etc.) now
that it's meant to work without korecord installed at all."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from korecord.compress import compress_bytes_to_file

SCRIPT = Path(__file__).parent.parent / "scripts" / "decrypt-recording.py"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


def test_decrypts_a_real_encrypted_file_byte_for_byte(tmp_path):
    original = b"some very secret terminal session content\n" * 50
    encrypted_path = tmp_path / "session.txt.zst.enc"
    compress_bytes_to_file(original, encrypted_path, password="correct-horse-battery-staple")

    output_path = tmp_path / "recovered.txt"
    env = {**os.environ, "KORECORD_PASSWORD": "correct-horse-battery-staple"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(encrypted_path), str(output_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert output_path.read_bytes() == original


def test_default_output_path_strips_enc_and_zst(tmp_path):
    original = b"content"
    encrypted_path = tmp_path / "session.txt.zst.enc"
    compress_bytes_to_file(original, encrypted_path, password="hunter2")

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(encrypted_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected_output = tmp_path / "session.txt"
    assert expected_output.exists()
    assert expected_output.read_bytes() == original


def test_wrong_password_fails_cleanly_not_with_a_traceback(tmp_path):
    encrypted_path = tmp_path / "session.txt.zst.enc"
    compress_bytes_to_file(b"secret", encrypted_path, password="right-password")

    env = {**os.environ, "KORECORD_PASSWORD": "wrong-password"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(encrypted_path), str(tmp_path / "out")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "Decryption failed" in result.stderr
    assert not (tmp_path / "out").exists()


def test_truncated_file_fails_cleanly(tmp_path):
    encrypted_path = tmp_path / "session.txt.zst.enc"
    encrypted_path.write_bytes(b"way too short")
    env = {**os.environ, "KORECORD_PASSWORD": "x"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(encrypted_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "too short" in result.stderr.lower()


def test_no_salt_argument_needed_since_its_embedded_in_the_file(tmp_path):
    """The whole point of the redesign this script now mirrors: earlier it
    took a separate salt-hex argument (copied out of config.json) -- if
    that file was ever lost, decryption was stuck even with the right
    password. Now the salt travels embedded in the file itself, so the
    script's own interface only needs the file and the password -- there's
    no salt argument to even pass anymore."""
    original = b"self-contained content"
    encrypted_path = tmp_path / "session.txt.zst.enc"
    compress_bytes_to_file(original, encrypted_path, password="hunter2")

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(encrypted_path), str(tmp_path / "out")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out").read_bytes() == original


def test_matches_a_real_cast_recording_end_to_end(tmp_path):
    """Realistic content, not just a synthetic byte string -- a genuine
    zstd-compressed asciicast, encrypted the way record() actually does
    it, decrypted purely by the standalone script."""
    cast_content = (
        b'{"version":2,"width":80,"height":24}\n'
        b'[0.005, "o", "hello world\\r\\n"]\n'
    )
    encrypted_path = tmp_path / "session.cast.zst.enc"
    compress_bytes_to_file(cast_content, encrypted_path, password="hunter2")

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(encrypted_path), str(tmp_path / "session.cast")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "session.cast").read_bytes() == cast_content
