"""Runs scripts/decrypt-recording.py as a real subprocess against a .rec
archive built through korecord's own archive.py/compress.py -- guards
against the standalone script silently drifting out of sync with the
actual file format (tar container shape, embedded salt position/size,
scrypt params, nonce size, zstd magic-byte sniffing, etc.) now that it's
meant to work without korecord installed at all."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from korecord import archive
from korecord.compress import pack_bytes

SCRIPT = Path(__file__).parent.parent / "scripts" / "decrypt-recording.py"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


def test_decrypts_a_real_encrypted_archive_byte_for_byte(tmp_path):
    cast_content = b"some very secret terminal session content\n" * 50
    txt_content = b"0.0\tsome very secret transcript line\n"
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "cast", pack_bytes(cast_content, password="correct-horse-battery-staple"))
    archive.append(rec_path, "txt", pack_bytes(txt_content, password="correct-horse-battery-staple"))

    prefix = tmp_path / "recovered"
    env = {**os.environ, "KORECORD_PASSWORD": "correct-horse-battery-staple"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path), str(prefix)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert Path(f"{prefix}.cast").read_bytes() == cast_content
    assert Path(f"{prefix}.txt").read_bytes() == txt_content


def test_default_output_prefix_strips_rec_suffix(tmp_path):
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "cast", pack_bytes(b"content", password="hunter2"))

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected_output = tmp_path / "session.cast"
    assert expected_output.exists()
    assert expected_output.read_bytes() == b"content"


def test_only_decrypts_members_actually_present(tmp_path):
    """A session that finished before its background transcript render
    caught up has a "cast" member but no "txt" one yet -- the script must
    only write out what's actually there, not fail or fabricate a .txt."""
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "cast", pack_bytes(b"content", password="hunter2"))

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "session.cast").exists()
    assert not (tmp_path / "session.txt").exists()


def test_wrong_password_fails_cleanly_not_with_a_traceback(tmp_path):
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "cast", pack_bytes(b"secret", password="right-password"))

    env = {**os.environ, "KORECORD_PASSWORD": "wrong-password"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path), str(tmp_path / "out")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "Decryption" in result.stderr
    assert not (tmp_path / "out.cast").exists()


def test_not_a_tar_file_fails_cleanly(tmp_path):
    rec_path = tmp_path / "session.rec"
    rec_path.write_bytes(b"way too short, not a tar archive at all")
    env = {**os.environ, "KORECORD_PASSWORD": "x"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "valid .rec archive" in result.stderr


def test_archive_with_no_recognized_members_fails_cleanly(tmp_path):
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "something-else", b"data")
    env = {**os.environ, "KORECORD_PASSWORD": "x"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_no_salt_argument_needed_since_its_embedded_in_the_archive(tmp_path):
    """The whole point of the redesign this script mirrors: earlier it
    took a separate salt-hex argument (copied out of config.json) -- if
    that file was ever lost, decryption was stuck even with the right
    password. Now the salt travels embedded in each member itself, so the
    script's own interface only needs the file and the password -- there's
    no salt argument to even pass anymore."""
    original = b"self-contained content"
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "cast", pack_bytes(original, password="hunter2"))

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path), str(tmp_path / "out")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert Path(f"{tmp_path / 'out'}.cast").read_bytes() == original


def test_matches_a_real_cast_recording_end_to_end(tmp_path):
    """Realistic content, not just a synthetic byte string -- a genuine
    zstd-compressed asciicast, packed+encrypted the way record() actually
    does it, decrypted purely by the standalone script."""
    cast_content = (
        b'{"version":2,"width":80,"height":24}\n'
        b'[0.005, "o", "hello world\\r\\n"]\n'
    )
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "cast", pack_bytes(cast_content, password="hunter2"))

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path), str(tmp_path / "session")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "session.cast").read_bytes() == cast_content


def test_sniffs_uncompressed_member_correctly(tmp_path):
    """compressed=False sessions store the plain bytes straight after
    encryption, with no zstd frame -- the script has no DB to consult for
    that flag, so it must detect this itself (zstd's frame magic number)
    rather than assume every member is always zstd-compressed."""
    original = b"plain, never zstd-compressed content"
    rec_path = tmp_path / "session.rec"
    archive.create(rec_path, "cast", pack_bytes(original, compress=False, password="hunter2"))

    env = {**os.environ, "KORECORD_PASSWORD": "hunter2"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(rec_path), str(tmp_path / "out")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert Path(f"{tmp_path / 'out'}.cast").read_bytes() == original
