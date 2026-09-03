from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from korecord import archive, config, db
from korecord.compress import pack_bytes
from korecord.recorder import (
    _asciinema_major_version,
    _descendant_pids,
    _kill_process_tree,
    default_label,
    find_asciinema,
    play,
    raw_cast_path,
    sanitize,
    sanitize_cast_data,
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    else:
        return True


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_sanitize_strips_unsafe_characters():
    assert sanitize("my host!@# name") == "my_host____name"


def test_sanitize_empty_falls_back_to_unknown():
    assert sanitize("") == "unknown"


def test_sanitize_all_unsafe_characters_becomes_underscores_not_unknown():
    # "unknown" is only the fallback for a genuinely empty string -- an
    # all-punctuation string still sanitizes to (non-empty) underscores.
    assert sanitize("!!!") == "___"


def test_sanitize_keeps_safe_characters():
    assert sanitize("host-1.example_com") == "host-1.example_com"


def test_default_label_from_ssh_target():
    assert default_label(["ssh", "user@myhost.example.com"]) == "myhost.example.com"


def test_default_label_from_ssh_target_with_boolean_flag():
    assert default_label(["ssh", "-v", "myhost"]) == "myhost"


def test_default_label_misreads_a_value_taking_flags_argument():
    """Documents an existing limitation, not desired behavior: the target
    picker takes the first argument not starting with "-", with no idea
    that flags like -p take a separate value argument -- so `-p 2222`
    mislabels the session "2222" instead of "myhost"."""
    assert default_label(["ssh", "-p", "2222", "myhost"]) == "2222"


def test_default_label_from_mosh_target():
    assert default_label(["mosh", "someuser@remotebox"]) == "remotebox"


def test_default_label_falls_back_to_program_name():
    assert default_label(["bash", "-c", "echo hi"]) == "bash"


def test_default_label_empty_command():
    assert default_label([]) == "unknown"


def test_sanitize_cast_data_empty_returns_none():
    assert sanitize_cast_data(b"") is None


def test_sanitize_cast_data_no_valid_header_returns_none():
    assert sanitize_cast_data(b"not json\nmore garbage\n") is None


def test_sanitize_cast_data_keeps_all_complete_lines():
    header = json.dumps({"version": 2, "width": 80, "height": 24}).encode()
    l1 = json.dumps([0.0, "o", "a"]).encode()
    l2 = json.dumps([1.0, "o", "b"]).encode()
    data = header + b"\n" + l1 + b"\n" + l2 + b"\n"
    assert sanitize_cast_data(data) == data


def test_sanitize_cast_data_trims_incomplete_tail_line(capsys):
    header = json.dumps({"version": 2, "width": 80, "height": 24}).encode()
    good_line = json.dumps([0.0, "o", "hi"]).encode()
    truncated = b'[0.5, "o", "incomple'
    data = header + b"\n" + good_line + b"\n" + truncated
    result = sanitize_cast_data(data)
    assert result == header + b"\n" + good_line + b"\n"
    assert "isn't complete yet" in capsys.readouterr().err


# --- raw_cast_path -----------------------------------------------------------

def test_raw_cast_path_swaps_rec_suffix_for_cast():
    assert raw_cast_path("/data/foo/bar.rec") == Path("/data/foo/bar.cast")
    assert raw_cast_path(Path("/data/foo/bar.rec")) == Path("/data/foo/bar.cast")


# --- asciinema discovery/version gate ----------------------------------------
# korec dropped the old PyPI `asciinema` package (frozen at 2.4.0, the last
# release of the pre-rewrite Python line) in favor of shelling out to
# whatever asciinema 3.x binary the user installs themselves -- these guard
# against silently running against an incompatible version.

def test_asciinema_major_version_parses_modern_output(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": "asciinema 3.2.1\n"})(),
    )
    assert _asciinema_major_version("/usr/bin/asciinema") == 3


def test_asciinema_major_version_old_release_format_not_misparsed(monkeypatch):
    # The old 2.x line prints "asciinema, version 2.4.0" (with a comma) --
    # a different shape than 3.x's "asciinema 3.2.1". The parser only
    # understands the new shape, so this comes back as "can't tell" rather
    # than silently misparsing a wrong number out of it.
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": "asciinema, version 2.4.0\n"})(),
    )
    assert _asciinema_major_version("/usr/bin/asciinema") is None


def test_asciinema_major_version_command_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr("subprocess.run", boom)
    assert _asciinema_major_version("/nonexistent") is None


def test_find_asciinema_not_on_path_exits(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("korecord.recorder._FALLBACK_ASCIINEMA_PATHS", ())
    with pytest.raises(SystemExit):
        find_asciinema()


def test_find_asciinema_falls_back_to_well_known_install_locations(monkeypatch, tmp_path):
    """Regression test, caught for real: a Tilix profile's custom command
    (`korec record -- ssh ...`, per README "Terminal-emulator integration")
    execs korec directly, inheriting the bare $PATH the desktop session set
    up -- no ~/.local/bin, no ~/.cargo/bin, since those are normally
    appended by .bashrc/.zshrc, which a directly-exec'd custom command never
    sources. That made korec (and, transitively, every ssh session opened
    via that Tilix profile) fail with "'asciinema' not found on $PATH" even
    though it was correctly installed at ~/.local/bin/asciinema. Falling
    back to checking that path directly (see _FALLBACK_ASCIINEMA_PATHS)
    fixes it without depending on $PATH being complete."""
    fake = tmp_path / "asciinema"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("korecord.recorder._FALLBACK_ASCIINEMA_PATHS", (fake,))
    monkeypatch.setattr("korecord.recorder._asciinema_major_version", lambda path: 3)
    assert find_asciinema() == str(fake)


def test_find_asciinema_fallback_skips_missing_or_non_executable_candidates(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist" / "asciinema"
    not_executable = tmp_path / "asciinema-noexec"
    not_executable.write_text("not a real binary")
    not_executable.chmod(0o644)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("korecord.recorder._FALLBACK_ASCIINEMA_PATHS", (missing, not_executable))
    with pytest.raises(SystemExit):
        find_asciinema()


def test_find_asciinema_too_old_exits(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/asciinema")
    monkeypatch.setattr("korecord.recorder._asciinema_major_version", lambda path: 2)
    with pytest.raises(SystemExit):
        find_asciinema()


def test_find_asciinema_accepts_v3(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/asciinema")
    monkeypatch.setattr("korecord.recorder._asciinema_major_version", lambda path: 3)
    assert find_asciinema() == "/usr/bin/asciinema"


def test_find_asciinema_unknown_version_proceeds_anyway(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/asciinema")
    monkeypatch.setattr("korecord.recorder._asciinema_major_version", lambda path: None)
    assert find_asciinema() == "/usr/bin/asciinema"
    assert "couldn't determine" in capsys.readouterr().err


def test_find_asciinema_finds_the_real_installed_binary():
    """Integration check against whatever asciinema is actually installed
    on this machine -- catches a real PATH/version mismatch pure mocking
    can't."""
    path = find_asciinema()
    assert Path(path).exists()


# --- play() + encryption ------------------------------------------------

def _insert_finished_session(tmp_path, *, cast_bytes=b"unused cast bytes", password=None, encrypted=False):
    path = tmp_path / "c.rec"
    archive.create(path, "cast", pack_bytes(cast_bytes, password=password))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
        encrypted=encrypted,
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)
    return sid, path


def test_play_not_found_exits():
    with pytest.raises(SystemExit):
        play(999999)


def test_play_encrypted_session_without_password_exits(tmp_path):
    sid, _ = _insert_finished_session(tmp_path, password="hunter2", encrypted=True)
    # encryption never configured in this test, pytest's stdin isn't a
    # tty -- nothing to try, nothing to prompt for
    with pytest.raises(SystemExit):
        play(sid)


def test_play_encrypted_session_with_stored_password(tmp_path, monkeypatch):
    """play() shells out to `asciinema play`, which needs a real terminal
    -- not exercised end-to-end here (see test_cli.py's real e2e tests for
    that); this only confirms play() gets *past* decryption successfully
    rather than exiting on a wrong/missing password."""
    config.set_encryption(enabled=True, password="hunter2", store_password=True)
    sid, _ = _insert_finished_session(
        tmp_path, cast_bytes=b'{"version":2,"width":80,"height":24}\n', password="hunter2", encrypted=True,
    )
    calls = []
    monkeypatch.setattr("korecord.recorder.find_asciinema", lambda: "true")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: calls.append((cmd, kw)))
    play(sid)
    assert calls, "expected play() to reach the point of invoking asciinema"


def test_play_encrypted_session_retries_with_a_different_password(tmp_path, monkeypatch):
    """Sessions can each have their own password -- play() must offer the
    same interactive retry grep/cat/decrypt already get, not just fail."""
    config.set_encryption(enabled=True, password="the-usual-password", store_password=True)
    sid, _ = _insert_finished_session(
        tmp_path, cast_bytes=b'{"version":2,"width":80,"height":24}\n',
        password="a-completely-different-password", encrypted=True,
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-completely-different-password")
    monkeypatch.setattr("korecord.recorder.find_asciinema", lambda: "true")
    play(sid)  # must get past decryption without raising


def test_play_encrypted_session_missing_cast_file_exits(tmp_path):
    """A password *is* available (so it gets past the encryption check),
    but the cast file itself is gone -- must still fail cleanly, not with
    a confusing decryption error about a file that isn't there."""
    config.set_encryption(enabled=True, password="hunter2", store_password=True)
    sid, path = _insert_finished_session(tmp_path, password="hunter2", encrypted=True)
    path.unlink()
    with pytest.raises(SystemExit):
        play(sid)


# --- process-tree teardown (SIGTERM/SIGHUP orphan cleanup) ----------------
#
# Regression tests for a real, confirmed incident: korec's own process
# dying abnormally (crashed window manager, closed terminal tab, OOM --
# anything that isn't a clean exit) used to leave the asciinema process it
# spawned, and whatever asciinema itself was running (e.g. ssh), as
# permanent orphans -- asciinema deliberately ignores SIGTERM/SIGHUP so a
# stray signal doesn't lose an in-progress recording, which is exactly
# what let it survive korec's own death. Confirmed live against a real
# leftover orphan: a dead terminal, korec long gone, asciinema+ssh still
# holding a connection open indefinitely.

def test_descendant_pids_finds_direct_children():
    proc = subprocess.Popen(["bash", "-c", "sleep 50 & sleep 50 & wait"])
    try:
        assert _wait_until(lambda: len(_descendant_pids(proc.pid)) == 2)
    finally:
        _kill_process_tree(proc.pid)
        proc.wait(timeout=5)


def test_descendant_pids_finds_nested_grandchildren():
    # The inner command must not be a lone simple command -- bash execs
    # straight into a single trailing simple command instead of forking it,
    # which would collapse "inner bash + sleep" into just one process and
    # defeat the point of this test.
    proc = subprocess.Popen(["bash", "-c", "bash -c 'sleep 50 & wait' & wait"])
    try:
        assert _wait_until(lambda: len(_descendant_pids(proc.pid)) == 2)  # inner bash + sleep
    finally:
        _kill_process_tree(proc.pid)
        proc.wait(timeout=5)


def test_descendant_pids_empty_for_leaf_process():
    proc = subprocess.Popen(["sleep", "50"])
    try:
        assert _descendant_pids(proc.pid) == []
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_descendant_pids_empty_for_nonexistent_pid():
    assert _descendant_pids(2**30) == []


def test_kill_process_tree_kills_every_descendant():
    proc = subprocess.Popen(["bash", "-c", "sleep 50 & sleep 50 & wait"])
    assert _wait_until(lambda: len(_descendant_pids(proc.pid)) == 2)
    children = _descendant_pids(proc.pid)

    _kill_process_tree(proc.pid)
    proc.wait(timeout=5)

    assert not _pid_alive(proc.pid)
    for child_pid in children:
        assert _wait_until(lambda p=child_pid: not _pid_alive(p)), f"pid {child_pid} survived"


def test_kill_process_tree_kills_nested_grandchildren():
    proc = subprocess.Popen(["bash", "-c", "bash -c 'sleep 50 & wait' & wait"])
    assert _wait_until(lambda: len(_descendant_pids(proc.pid)) == 2)
    descendants = _descendant_pids(proc.pid)

    _kill_process_tree(proc.pid)
    proc.wait(timeout=5)

    for pid in [proc.pid] + descendants:
        assert _wait_until(lambda p=pid: not _pid_alive(p)), f"pid {pid} survived"


def test_kill_process_tree_tolerates_already_dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    # proc.pid is already reaped and gone -- must not raise
    _kill_process_tree(proc.pid)
