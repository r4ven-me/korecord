from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from rich.console import Console

import korecord
from korecord import archive, db, ui
from korecord.cli import (
    build_parser,
    cmd_clear,
    cmd_config_compression_show,
    cmd_config_encryption_show,
    cmd_config_show,
    cmd_rm,
)
from korecord.compress import pack_bytes, unpack_bytes
from korecord.recorder import _descendant_pids


def _force_terminal_console(monkeypatch) -> Console:
    """See test_db.py's copy of this helper for why -- same technique,
    duplicated rather than shared since it's a few lines and these two
    test modules don't otherwise import from each other."""
    console = Console(force_terminal=True, width=200, file=io.StringIO())
    monkeypatch.setattr(ui, "console", console)
    return console


def _parse(argv):
    return build_parser().parse_args(argv)


def _insert_finished_session(tmp_path, *, name="s"):
    path = tmp_path / f"{name}.rec"
    archive.create(path, "cast", pack_bytes(b"unused"))
    archive.append(path, "txt", pack_bytes(b"unused"))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=1, cast_size=1, exit_code=0)
    return sid, path


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


def test_help_exits_zero(tmp_path):
    env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "korecord", "--help"],
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert result.returncode == 0
    assert "korec" in result.stdout


def test_version_flag_prints_version_and_exits_zero(tmp_path):
    env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "korecord", "--version"],
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"korec {korecord.__version__}"


def test_unknown_subcommand_fails_cleanly(tmp_path):
    env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "korecord", "not-a-real-subcommand"],
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


# --- regression: `korec cat <id> | less`, then quitting `less` before EOF
# used to crash with a BrokenPipeError traceback. cli.main() now restores
# the default SIGPIPE disposition so the process dies quietly, like any
# other Unix tool piping into a pager that closes early. -----------------
def test_cat_piped_into_early_exit_reader_dies_quietly_via_sigpipe(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    path = tmp_path / "c.rec"
    archive.create(path, "cast", pack_bytes(b"unused"))
    # Enough lines that `head -n 1` will have exited and closed the pipe
    # long before `korec cat` finishes writing everything.
    txt = "".join(f"{float(i)}\tline {i}\n" for i in range(20000))
    archive.append(path, "txt", pack_bytes(txt.encode()))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)

    env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path)}
    cat_proc = subprocess.Popen(
        [sys.executable, "-m", "korecord", "cat", str(sid)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    head_proc = subprocess.Popen(["head", "-n", "1"], stdin=cat_proc.stdout, stdout=subprocess.PIPE)
    cat_proc.stdout.close()  # let cat_proc see SIGPIPE once head_proc exits

    head_out = head_proc.communicate(timeout=15)[0]
    _, cat_err = cat_proc.communicate(timeout=15)

    assert head_out == b"[2026-01-01 10:00:00] line 0\n"
    assert cat_proc.returncode == -signal.SIGPIPE
    assert b"Traceback" not in cat_err
    assert b"BrokenPipeError" not in cat_err


def test_grep_piped_into_early_exit_reader_dies_quietly_via_sigpipe(tmp_path, monkeypatch):
    """Same SIGPIPE fix, exercised through `korec grep` (matches across
    many sessions) rather than `korec cat`."""
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    # Enough sessions/output that the OS pipe buffer (~64KB) fills before
    # `head -n 1` reads its one line and exits -- otherwise grep_proc can
    # finish writing (and exit 0) before the pipe ever gets closed on it.
    for i in range(1500):
        path = tmp_path / f"c{i}.rec"
        archive.create(path, "cast", pack_bytes(b"unused"))
        archive.append(path, "txt", pack_bytes(f"0.0\tmarker line {i}\n".encode()))
        sid = db.insert_pending_session(
            start=f"2026-01-01T10:{i % 60:02d}:00+00:00", local_host="l", remote_host="r",
            tty="t", path=str(path), pid=os.getpid(),
        )
        db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)

    env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path)}
    grep_proc = subprocess.Popen(
        [sys.executable, "-m", "korecord", "grep", "marker"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    head_proc = subprocess.Popen(["head", "-n", "1"], stdin=grep_proc.stdout, stdout=subprocess.PIPE)
    grep_proc.stdout.close()

    head_proc.communicate(timeout=15)
    _, grep_err = grep_proc.communicate(timeout=15)

    assert grep_proc.returncode == -signal.SIGPIPE
    assert b"Traceback" not in grep_err


# --- full pipeline smoke test -------------------------------------------

def test_end_to_end_record_grep_cat(tmp_path):
    """Record a short real command, wait for it to finish and for the
    detached background renderer to build its transcript, then confirm
    `grep` finds it and `cat` replays it with a timestamp attached."""
    env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path)}
    marker = "E2E_MARKER_UNIQUE_STRING"

    record_result = subprocess.run(
        [sys.executable, "-m", "korecord", "record", "--label", "e2etest", "--",
         "bash", "-c", f"echo {marker}"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert record_result.returncode == 0, record_result.stderr

    deadline = time.monotonic() + 15
    grep_result = None
    while time.monotonic() < deadline:
        grep_result = subprocess.run(
            [sys.executable, "-m", "korecord", "grep", marker],
            capture_output=True, text=True, env=env, timeout=15,
        )
        if grep_result.returncode == 0:
            break
        time.sleep(0.2)

    assert grep_result is not None and grep_result.returncode == 0, (
        f"background render never produced a searchable transcript in time; "
        f"last grep stderr: {grep_result.stderr if grep_result else '<none>'}"
    )
    assert marker in grep_result.stdout
    assert "e2etest" in grep_result.stdout

    ls_result = subprocess.run(
        [sys.executable, "-m", "korecord", "ls"],
        capture_output=True, text=True, env=env, timeout=15,
    )
    session_id = None
    for line in ls_result.stdout.splitlines()[1:]:
        if "e2etest" in line:
            session_id = line.split()[0]
            break
    assert session_id is not None, ls_result.stdout

    cat_result = subprocess.run(
        [sys.executable, "-m", "korecord", "cat", session_id],
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert cat_result.returncode == 0
    assert marker in cat_result.stdout
    assert cat_result.stdout.startswith("[")  # a timestamp, not "[?]"
    assert "[?]" not in cat_result.stdout


# --- full pipeline smoke test, encrypted ---------------------------------

def test_end_to_end_encrypted_record_grep_cat(tmp_path):
    """Full pipeline with encryption on: `korec config encryption enable`
    (non-interactively, via $KORECORD_PASSWORD), record a real session,
    confirm grep/cat transparently decrypt it with the *stored* config
    password (not relying on $KORECORD_PASSWORD still being set), and --
    the actual point of the feature -- confirm what's on disk is genuinely
    unreadable without the key, not just zstd-compressed."""
    config_dir = tmp_path / "config"
    base_env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(config_dir)}
    marker = "ENCRYPTED_E2E_MARKER"
    password = "correct-horse-battery-staple"

    enable_env = {**base_env, "KORECORD_PASSWORD": password}
    enable_result = subprocess.run(
        [sys.executable, "-m", "korecord", "config", "encryption", "enable", "--store-password"],
        capture_output=True, text=True, env=enable_env, timeout=15,
    )
    assert enable_result.returncode == 0, enable_result.stderr
    assert "enabled" in enable_result.stdout.lower()

    show_result = subprocess.run(
        [sys.executable, "-m", "korecord", "config", "encryption", "show"],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert "enabled" in show_result.stdout
    assert "stored in config file: yes" in show_result.stdout

    # Deliberately NOT passing $KORECORD_PASSWORD from here on -- proves
    # `record` and `grep`/`cat` pick the password up from the config file,
    # not a lingering env var.
    record_result = subprocess.run(
        [sys.executable, "-m", "korecord", "record", "--label", "enctest", "--",
         "bash", "-c", f"echo {marker}"],
        capture_output=True, text=True, env=base_env, timeout=30,
    )
    assert record_result.returncode == 0, record_result.stderr

    deadline = time.monotonic() + 15
    grep_result = None
    while time.monotonic() < deadline:
        grep_result = subprocess.run(
            [sys.executable, "-m", "korecord", "grep", marker],
            capture_output=True, text=True, env=base_env, timeout=15,
        )
        if grep_result.returncode == 0:
            break
        time.sleep(0.2)
    assert grep_result is not None and grep_result.returncode == 0, (
        grep_result.stderr if grep_result else "<no grep result>"
    )
    assert marker in grep_result.stdout

    ls_result = subprocess.run(
        [sys.executable, "-m", "korecord", "ls"],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    session_id = None
    for line in ls_result.stdout.splitlines()[1:]:
        if "enctest" in line:
            session_id = line.split()[0]
            break
    assert session_id is not None, ls_result.stdout

    # The ENC/COMPRESSED markers in `ls` are cosmetic (and, being blank
    # rather than a token when off, not reliably positional to parse out
    # of that table) -- `show`'s `encrypted` field is the actual source
    # of truth this test cares about.
    show_result = subprocess.run(
        [sys.executable, "-m", "korecord", "show", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert "\nencrypted: 1" in show_result.stdout

    cat_result = subprocess.run(
        [sys.executable, "-m", "korecord", "cat", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert cat_result.returncode == 0
    assert marker in cat_result.stdout

    # The actual point: what's on disk must not be plain zstd. Decrypting
    # without a key must fail outright, not just fail to contain the
    # marker as a substring (compressed data wouldn't contain it either).
    # Unlike the old per-file ".enc" suffix scheme, whether a session is
    # encrypted no longer shows up in its filename at all (every session
    # is just `<base>.rec`) -- only the index (`encrypted` field) and,
    # deliberately, an actual decrypt attempt can tell.
    show_meta = subprocess.run(
        [sys.executable, "-m", "korecord", "show", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    rec_path = next(
        line.split(": ", 1)[1] for line in show_meta.stdout.splitlines() if line.startswith("path:")
    )
    assert rec_path.endswith(".rec")
    assert "\nencrypted: 1" in show_meta.stdout
    with pytest.raises(Exception):
        unpack_bytes(archive.read_member(Path(rec_path), "cast"))  # no password -- must not silently succeed

    # Wrong password fails clearly rather than corrupting/hanging.
    wrong_env = {**base_env, "KORECORD_PASSWORD": "not-the-right-password"}
    wrong_pw_result = subprocess.run(
        [sys.executable, "-m", "korecord", "grep", "--session", session_id, marker],
        capture_output=True, text=True, env=wrong_env, timeout=15,
    )
    assert wrong_pw_result.returncode != 0
    assert "didn't decrypt" in wrong_pw_result.stderr

    disable_result = subprocess.run(
        [sys.executable, "-m", "korecord", "config", "encryption", "disable"],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert disable_result.returncode == 0
    # Disabling must not forget the stored password -- the already
    # -encrypted session above must stay readable afterwards.
    cat_after_disable = subprocess.run(
        [sys.executable, "-m", "korecord", "cat", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert cat_after_disable.returncode == 0
    assert marker in cat_after_disable.stdout


def test_decrypt_and_encrypt_cli_roundtrip(tmp_path):
    """`korec decrypt <id>` / `korec encrypt <id>` exercised as real CLI
    subcommands against a real recorded, encrypted session -- not just the
    underlying db.py functions directly."""
    config_dir = tmp_path / "config"
    base_env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(config_dir)}
    marker = "DECRYPT_CLI_MARKER"
    password = "another-strong-password"

    enable_env = {**base_env, "KORECORD_PASSWORD": password}
    enable_result = subprocess.run(
        [sys.executable, "-m", "korecord", "config", "encryption", "enable", "--store-password"],
        capture_output=True, text=True, env=enable_env, timeout=15,
    )
    assert enable_result.returncode == 0, enable_result.stderr

    record_result = subprocess.run(
        [sys.executable, "-m", "korecord", "record", "--label", "dectest", "--",
         "bash", "-c", f"echo {marker}"],
        capture_output=True, text=True, env=base_env, timeout=30,
    )
    assert record_result.returncode == 0, record_result.stderr

    ls_result = subprocess.run(
        [sys.executable, "-m", "korecord", "ls"], capture_output=True, text=True, env=base_env, timeout=15,
    )
    session_id = next(line.split()[0] for line in ls_result.stdout.splitlines()[1:] if "dectest" in line)

    def field(show_stdout: str, name: str) -> str:
        return next(l.split(": ", 1)[1] for l in show_stdout.splitlines() if l.startswith(f"{name}:"))

    deadline = time.monotonic() + 15
    rec_path = None
    while time.monotonic() < deadline:
        show_result = subprocess.run(
            [sys.executable, "-m", "korecord", "show", session_id],
            capture_output=True, text=True, env=base_env, timeout=15,
        )
        rec_path = Path(field(show_result.stdout, "path"))
        if rec_path.exists() and archive.has_member(rec_path, "txt"):
            break
        time.sleep(0.2)
    else:
        pytest.fail("background transcript render never finished")

    decrypt_result = subprocess.run(
        [sys.executable, "-m", "korecord", "decrypt", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert decrypt_result.returncode == 0, decrypt_result.stderr
    assert "decrypted in place" in decrypt_result.stdout

    show_after = subprocess.run(
        [sys.executable, "-m", "korecord", "show", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    ).stdout
    assert field(show_after, "encrypted") == "0"
    assert field(show_after, "path") == str(rec_path)  # no rename

    # readable with NO password available at all now
    no_password_env = {k: v for k, v in base_env.items() if k != "KORECORD_PASSWORD"}
    cat_no_pw = subprocess.run(
        [sys.executable, "-m", "korecord", "cat", session_id],
        capture_output=True, text=True, env=no_password_env, timeout=15,
    )
    assert cat_no_pw.returncode == 0
    assert marker in cat_no_pw.stdout

    encrypt_result = subprocess.run(
        [sys.executable, "-m", "korecord", "encrypt", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert encrypt_result.returncode == 0, encrypt_result.stderr
    assert "encrypted in place" in encrypt_result.stdout

    show_final = subprocess.run(
        [sys.executable, "-m", "korecord", "show", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    ).stdout
    assert field(show_final, "encrypted") == "1"
    assert field(show_final, "path") == str(rec_path)  # no rename

    cat_final = subprocess.run(
        [sys.executable, "-m", "korecord", "cat", session_id],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert cat_final.returncode == 0
    assert marker in cat_final.stdout


# Each encrypted file carries its own salt, embedded directly in it (see
# crypto.py) -- config.json holds only the password, so losing/recreating
# it and re-entering the exact same password must still decrypt every
# already-encrypted session, with nothing else to recover or restore.

def test_lost_config_file_recovers_with_same_password(tmp_path):
    config_dir = tmp_path / "config"
    base_env = {**os.environ, "KORECORD_DATA_DIR": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(config_dir)}
    marker = "SURVIVES_LOST_CONFIG_MARKER"
    password = "correct-horse-battery-staple"

    enable_env = {**base_env, "KORECORD_PASSWORD": password}
    assert subprocess.run(
        [sys.executable, "-m", "korecord", "config", "encryption", "enable", "--store-password"],
        capture_output=True, text=True, env=enable_env, timeout=15,
    ).returncode == 0

    record_result = subprocess.run(
        [sys.executable, "-m", "korecord", "record", "--label", "losttest", "--",
         "bash", "-c", f"echo {marker}"],
        capture_output=True, text=True, env=base_env, timeout=30,
    )
    assert record_result.returncode == 0, record_result.stderr

    deadline = time.monotonic() + 15
    grep_result = None
    while time.monotonic() < deadline:
        grep_result = subprocess.run(
            [sys.executable, "-m", "korecord", "grep", marker],
            capture_output=True, text=True, env=base_env, timeout=15,
        )
        if grep_result.returncode == 0:
            break
        time.sleep(0.2)
    assert grep_result is not None and grep_result.returncode == 0, (
        grep_result.stderr if grep_result else "<no grep result>"
    )

    # config.json (and whatever it held) is just gone.
    (config_dir / "korecord" / "config.json").unlink()

    # Re-enable with the exact same password, exactly like a user
    # recovering from "I lost my config file".
    reenable_result = subprocess.run(
        [sys.executable, "-m", "korecord", "config", "encryption", "enable", "--store-password"],
        capture_output=True, text=True, env=enable_env, timeout=15,
    )
    assert reenable_result.returncode == 0, reenable_result.stderr

    # The previously-encrypted session must still be fully readable --
    # no special recovery step, no leftover warning, nothing lost.
    grep_after = subprocess.run(
        [sys.executable, "-m", "korecord", "grep", marker],
        capture_output=True, text=True, env=base_env, timeout=15,
    )
    assert grep_after.returncode == 0, grep_after.stderr
    assert marker in grep_after.stdout


# --- regression: SIGTERM/SIGHUP to `korec record` used to leave asciinema
# (and whatever it was recording, e.g. ssh) running forever -- confirmed on
# a real orphaned session after a crashed window manager. asciinema
# deliberately ignores SIGTERM/SIGHUP itself (so a stray signal doesn't cut
# off an in-progress recording), which is exactly what let it survive
# korec's own death. This test stands in a fake `asciinema` that reproduces
# that exact behavior -- signal-ignoring, spawns the recorded command via a
# shell, same argv shape -- so the test exercises korec's own teardown
# logic without depending on a real terminal/pty, which a real asciinema
# invocation needs and a plain subprocess pipe can't provide. -------------

_FAKE_ASCIINEMA = """\
#!/usr/bin/env python3
import signal
import subprocess
import sys

if sys.argv[1:2] == ["--version"]:
    print("asciinema 3.99.0")
    sys.exit(0)

args = sys.argv[1:]
cmd = args[args.index("-c") + 1]
raw_path = args[-1]

# The exact behavior that lets real asciinema survive its parent dying:
# these signals are simply ignored.
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)

with open(raw_path, "w") as f:
    f.write('{"version": 2, "width": 80, "height": 24}\\n')

proc = subprocess.Popen(["sh", "-c", cmd])
sys.exit(proc.wait())
"""


def test_sigterm_to_record_kills_the_whole_recording_tree_and_finalizes_session(tmp_path, monkeypatch):
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_asciinema = fake_bin / "asciinema"
    fake_asciinema.write_text(_FAKE_ASCIINEMA)
    fake_asciinema.chmod(0o755)

    env = {
        **os.environ,
        "KORECORD_DATA_DIR": str(tmp_path / "data"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "korecord", "record", "--label", "sigtermtest", "--",
         "bash", "-c", "sleep 100 & echo ready; wait"],
        env=env, stdout=subprocess.PIPE, text=True,
    )
    try:
        # Block until the recorded command is actually running -- "ready"
        # is only echoed once the background `sleep` has been launched --
        # so there's a real tree in place before the signal is sent.
        assert proc.stdout.readline().strip() == "ready"

        descendants: list[int] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(descendants) < 3:
            descendants = _descendant_pids(proc.pid)
            time.sleep(0.1)
        # fake asciinema, sh, bash, sleep -- at least 3 of that chain must
        # have shown up under /proc by now.
        assert len(descendants) >= 3, f"recording tree never fully started: {descendants}"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)

        for pid in descendants:
            assert _wait_until(lambda p=pid: not _pid_alive(p)), \
                f"pid {pid} survived SIGTERM to korec record"
    finally:
        proc.stdout.close()

    # A single, fresh KORECORD_DATA_DIR -- this must be the session's id.
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path / "data"))
    row = db.get_session(1)
    assert row is not None
    assert row["end_time"] is not None, "session was never finalized after the kill"
    assert row["exit_code"] == -9


def test_record_lays_out_files_under_a_per_day_folder(tmp_path):
    """Recordings are grouped <label>/<year>/<month>/<day>/ -- a day-level
    folder on top of the previous <year>/<month> layout, so a label used
    daily (e.g. a frequently-ssh'd host) doesn't dump every session for the
    whole month into one flat directory."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_asciinema = fake_bin / "asciinema"
    fake_asciinema.write_text(_FAKE_ASCIINEMA)
    fake_asciinema.chmod(0o755)

    data_dir = tmp_path / "data"
    env = {
        **os.environ,
        "KORECORD_DATA_DIR": str(data_dir),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    today = datetime.now().astimezone()
    result = subprocess.run(
        [sys.executable, "-m", "korecord", "record", "--label", "daytest", "--", "true"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr

    expected_dir = data_dir / "daytest" / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
    assert expected_dir.is_dir(), f"expected {expected_dir} to exist"
    assert list(expected_dir.glob("*.rec")), "no .rec archive landed in the per-day folder"


# --- `korec rm` / `korec clear` -------------------------------------------

def test_rm_deletes_session_files_and_row(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    sid, path = _insert_finished_session(tmp_path)

    cmd_rm(_parse(["rm", str(sid)]))

    assert db.get_session(sid) is None
    assert not path.exists()
    assert f"session {sid} deleted" in capsys.readouterr().out


def test_rm_multiple_ids_deletes_all(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    sid1, _ = _insert_finished_session(tmp_path, name="a")
    sid2, _ = _insert_finished_session(tmp_path, name="b")

    cmd_rm(_parse(["rm", str(sid1), str(sid2)]))

    assert db.get_session(sid1) is None
    assert db.get_session(sid2) is None


def test_rm_unknown_id_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        cmd_rm(_parse(["rm", "999999"]))


def test_clear_with_no_sessions_is_a_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    cmd_clear(_parse(["clear"]))
    assert "no sessions to delete" in capsys.readouterr().out


def test_clear_yes_flag_skips_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    sid, path = _insert_finished_session(tmp_path)

    cmd_clear(_parse(["clear", "--yes"]))

    assert db.get_session(sid) is None
    assert not path.exists()


def test_clear_resets_ids_so_the_next_session_starts_at_one_again(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    _insert_finished_session(tmp_path, name="a")
    _insert_finished_session(tmp_path, name="b")

    cmd_clear(_parse(["clear", "--yes"]))

    new_sid, _ = _insert_finished_session(tmp_path, name="after")
    assert new_sid == 1


def test_clear_non_interactive_without_yes_refuses(tmp_path, monkeypatch):
    """No tty to prompt on and no --yes given -- refuse rather than either
    silently deleting everything or hanging on a read from a closed
    stdin."""
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    sid, _ = _insert_finished_session(tmp_path)

    with pytest.raises(SystemExit):
        cmd_clear(_parse(["clear"]))
    assert db.get_session(sid) is not None


def test_clear_interactive_prompt_declined_leaves_sessions_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    sid, _ = _insert_finished_session(tmp_path)

    cmd_clear(_parse(["clear"]))

    assert db.get_session(sid) is not None


def test_clear_interactive_prompt_confirmed_deletes_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    sid1, _ = _insert_finished_session(tmp_path, name="a")
    sid2, _ = _insert_finished_session(tmp_path, name="b")

    cmd_clear(_parse(["clear"]))

    assert db.get_session(sid1) is None
    assert db.get_session(sid2) is None


def test_clear_skips_running_session_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    finished_sid, _ = _insert_finished_session(tmp_path, name="fin")
    raw_path = tmp_path / "run.cast"
    raw_path.write_bytes(b'{"version": 2, "width": 80, "height": 24}\n')
    running_sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(tmp_path / "run.rec"), pid=os.getpid(),
    )

    cmd_clear(_parse(["clear", "--yes"]))

    assert db.get_session(finished_sid) is None
    assert db.get_session(running_sid) is not None
    assert "skipped 1 still recording" in capsys.readouterr().out


def test_clear_force_deletes_running_sessions_too(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    raw_path = tmp_path / "run.cast"
    raw_path.write_bytes(b'{"version": 2, "width": 80, "height": 24}\n')
    running_sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(tmp_path / "run.rec"), pid=os.getpid(),
    )

    cmd_clear(_parse(["clear", "--yes", "--force"]))

    assert db.get_session(running_sid) is None
    assert not raw_path.exists()


# --- pretty (rich) vs. plain output ----------------------------------------
#
# `korec ls`/`show` have their own dedicated coverage in test_db.py (that's
# where the actual rendering lives); these just confirm the config-status
# commands in cli.py follow the same is_terminal-gated pattern.

def test_config_show_pretty_mode_renders_a_boxed_table(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    console = _force_terminal_console(monkeypatch)

    cmd_config_show(_parse(["config", "show"]))

    out = console.file.getvalue()
    assert "\x1b[" in out
    assert "╭" in out and "╰" in out
    assert "config file" in out


def test_config_encryption_show_pretty_mode_renders_a_boxed_table(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    console = _force_terminal_console(monkeypatch)

    cmd_config_encryption_show(_parse(["config", "encryption", "show"]))

    out = console.file.getvalue()
    assert "\x1b[" in out
    assert "╭" in out and "╰" in out
    assert "disabled" in out


def test_config_compression_show_pretty_mode_renders_a_boxed_table(tmp_path, monkeypatch):
    monkeypatch.setenv("KORECORD_DATA_DIR", str(tmp_path))
    console = _force_terminal_console(monkeypatch)

    cmd_config_compression_show(_parse(["config", "compression", "show"]))

    out = console.file.getvalue()
    assert "\x1b[" in out
    assert "╭" in out and "╰" in out
    assert "enabled" in out
