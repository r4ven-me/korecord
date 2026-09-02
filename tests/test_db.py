from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from rich.console import Console

from korecord import archive, config, db, ui
from korecord.compress import pack_bytes, unpack_bytes
from korecord.recorder import raw_cast_path


def _force_terminal_console(monkeypatch) -> Console:
    """Rich's `Console.is_terminal` is a read-only property (auto-detected
    from the real file), so it can't be monkeypatched directly -- swap in
    a whole fresh Console pinned to force_terminal=True instead, writing
    to an in-memory buffer this can then inspect. db.py/cli.py always look
    up `ui.console` through the module (not a locally-bound copy), so
    patching this module attribute is enough to redirect them."""
    console = Console(force_terminal=True, width=200, file=io.StringIO())
    monkeypatch.setattr(ui, "console", console)
    return console


def _make_cast_bytes(events, width=80, height=24):
    header = json.dumps({"version": 2, "width": width, "height": height})
    lines = [header] + [json.dumps(list(e)) for e in events]
    return ("\n".join(lines) + "\n").encode()


def _insert_finished_session(
    tmp_path,
    *,
    name="s",
    start="2026-01-01T10:00:00+00:00",
    remote_host="remotehost",
    content_lines=("hello world",),
    exit_code=0,
):
    """A session with a ready "txt" archive member, as if `_render` already
    ran."""
    path = tmp_path / f"{name}.rec"
    archive.create(path, "cast", pack_bytes(b"unused"))
    txt = "".join(f"{float(i)}\t{line}\n" for i, line in enumerate(content_lines))
    archive.append(path, "txt", pack_bytes(txt.encode()))
    sid = db.insert_pending_session(
        start=start, local_host="local", remote_host=remote_host,
        tty="pts_1", path=str(path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:05:00+00:00", duration=300, cast_size=10, exit_code=exit_code)
    return sid


def _insert_running_session(tmp_path, *, name="r", start="2026-01-01T10:00:00+00:00", events=()):
    """A session still recording: end_time is NULL, no .rec archive yet --
    asciinema (3.x) writes straight to the plain, uncompressed "raw" file
    (see recorder.raw_cast_path) until the session ends and `record()`
    packs it into the archive. That's the only place data actually exists
    while a session is live."""
    path = tmp_path / f"{name}.rec"  # deliberately never created
    raw_cast_path(path).write_bytes(_make_cast_bytes(events))
    return db.insert_pending_session(
        start=start, local_host="local", remote_host="remotehost",
        tty="pts_2", path=str(path), pid=os.getpid(),
    )


def _insert_running_session_with_stale_archive(tmp_path, *, name="rc", events=()):
    """Edge case: a .rec archive already exists (e.g. left over) while the
    session still shows as running -- _live_transcript should prefer it
    over the raw file rather than erroring or ignoring it."""
    path = tmp_path / f"{name}.rec"
    archive.create(path, "cast", pack_bytes(_make_cast_bytes(events)))
    return db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="local", remote_host="remotehost",
        tty="pts_3", path=str(path), pid=os.getpid(),
    )


def _insert_finished_encrypted_session(
    tmp_path, *, name="enc", start="2026-01-01T10:00:00+00:00", content_lines=("hello world",), password,
):
    """A finished, encrypted session -- both archive members packed with
    `password`, matching how record()/_render actually produce them when
    encryption is on. Each member gets its own independently-generated
    salt (see crypto.py), even though both use the same password here."""
    path = tmp_path / f"{name}.rec"
    archive.create(path, "cast", pack_bytes(b"unused", password=password))
    txt = "".join(f"{float(i)}\t{line}\n" for i, line in enumerate(content_lines))
    archive.append(path, "txt", pack_bytes(txt.encode(), password=password))
    sid = db.insert_pending_session(
        start=start, local_host="local", remote_host="remotehost",
        tty="pts_5", path=str(path), pid=os.getpid(), encrypted=True,
    )
    db.finalize_session(sid, end="2026-01-01T10:05:00+00:00", duration=300, cast_size=10, exit_code=0)
    return sid


def _insert_running_encrypted_session(tmp_path, *, name="encr", events=()):
    """A still-recording session flagged `encrypted` -- but its raw file
    (what asciinema actually writes live) is always plaintext regardless,
    since asciinema has no idea korec encrypts the finished artifact."""
    path = tmp_path / f"{name}.rec"  # deliberately never created
    raw_cast_path(path).write_bytes(_make_cast_bytes(events))
    return db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="local", remote_host="remotehost",
        tty="pts_6", path=str(path), pid=os.getpid(), encrypted=True,
    )


def _enable_encryption(password="hunter2"):
    config.set_encryption(enabled=True, password=password, store_password=True)
    return password


# --- _parse_transcript_line / _format_ts -----------------------------------

def test_parse_transcript_line_with_timestamp():
    assert db._parse_transcript_line("1.5\tsome text") == (1.5, "some text")


def test_parse_transcript_line_legacy_no_timestamp():
    """Sidecars rendered before per-line timestamps existed have no tab --
    must still be greppable/cat-able, just without a timestamp."""
    assert db._parse_transcript_line("plain old line") == (None, "plain old line")


def test_parse_transcript_line_unparseable_head_treated_as_legacy():
    assert db._parse_transcript_line("notanumber\tcontent") == (None, "notanumber\tcontent")


def test_format_ts_adds_elapsed_seconds_to_start():
    assert db._format_ts("2026-01-01T10:00:00+00:00", 65.0) == "2026-01-01 10:01:05"


def test_format_ts_unknown_elapsed_is_question_mark():
    assert db._format_ts("2026-01-01T10:00:00+00:00", None) == "?"


def test_format_ts_bad_start_is_question_mark():
    assert db._format_ts("not-a-timestamp", 5.0) == "?"


# --- session bookkeeping ----------------------------------------------------

def test_insert_and_finalize_session_roundtrip(tmp_path):
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="lh", remote_host="rh",
        tty="pts_5", path=str(tmp_path / "x.rec"), pid=os.getpid(),
    )
    row = db.get_session(sid)
    assert row["end_time"] is None
    assert row["remote_host"] == "rh"
    assert row["compressed"] == 1  # default

    db.finalize_session(sid, end="2026-01-01T10:10:00+00:00", duration=600, cast_size=123, exit_code=0)
    row = db.get_session(sid)
    assert row["end_time"] == "2026-01-01T10:10:00+00:00"
    assert row["exit_code"] == 0
    assert row["cast_size"] == 123


def test_insert_session_with_compression_disabled(tmp_path):
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="lh", remote_host="rh",
        tty="pts_5", path=str(tmp_path / "x.rec"), pid=os.getpid(), compressed=False,
    )
    assert db.get_session(sid)["compressed"] == 0


def test_get_session_not_found_returns_none():
    assert db.get_session(123456) is None


def test_pid_alive_true_for_current_process():
    assert db._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_implausible_pid():
    assert db._pid_alive(2**30) is False


def test_pid_alive_false_for_none():
    assert db._pid_alive(None) is False


def test_status_running_when_pid_alive_and_unfinished():
    assert db._status(None, None, os.getpid()) == "RUNNING"


def test_status_killed_when_pid_dead_and_unfinished():
    assert db._status(None, None, 2**30) == "KILLED?"


def test_status_shows_exit_code_when_finished():
    assert db._status("2026-01-01T00:00:00", 0, None) == "0"
    assert db._status("2026-01-01T00:00:00", None, None) == "?"


def test_print_sessions_lists_rows(tmp_path, capsys):
    _insert_finished_session(tmp_path, remote_host="myhostname")
    db.print_sessions()
    out = capsys.readouterr().out
    assert "ID" in out
    assert "myhostname" in out


def test_print_sessions_shows_compressed_column(tmp_path, capsys):
    """Under pytest, stdout isn't a real terminal, so this exercises the
    plain (non-rich) fallback -- a fixed-width table script output can
    still rely on, now with COMPRESSED appended after ENC."""
    sid_compressed = _insert_finished_session(tmp_path, name="c", remote_host="compressedhost")
    path = tmp_path / "raw.rec"
    archive.create(path, "cast", pack_bytes(b"unused", compress=False))
    sid_raw = db.insert_pending_session(
        start="2026-01-01T09:00:00+00:00", local_host="l", remote_host="rawhost",
        tty="t", path=str(path), pid=os.getpid(), compressed=False,
    )
    db.finalize_session(sid_raw, end="2026-01-01T09:01:00+00:00", duration=60, cast_size=1, exit_code=0)

    db.print_sessions()
    out = capsys.readouterr().out
    assert "COMPRESSED" in out

    lines = {line.split()[3]: line for line in out.splitlines()[1:]}
    assert lines["compressedhost"].rstrip().endswith("*")  # compressed=True -- trailing marker
    assert not lines["rawhost"].rstrip().endswith("*")  # compressed=False -- no marker at all


def test_status_style_maps_each_status_to_a_color():
    assert db._status_style("RUNNING") == "yellow"
    assert db._status_style("KILLED?") == "bold red"
    assert db._status_style("0") == "green"
    assert db._status_style("?") == "dim"
    assert db._status_style("-9") == "red"
    assert db._status_style("255") == "red"


def test_print_sessions_pretty_mode_renders_a_boxed_colored_table(tmp_path, monkeypatch):
    """Connected to a real terminal, `ls` renders a rich table (box-drawing
    borders, ANSI color codes) instead of the plain fallback -- exercised
    here by forcing a Console that still writes somewhere inspectable
    (see _force_terminal_console) rather than an actual tty."""
    _insert_finished_session(tmp_path, remote_host="myhostname")
    console = _force_terminal_console(monkeypatch)

    db.print_sessions()

    out = console.file.getvalue()
    assert "\x1b[" in out  # actual ANSI escapes, not the plain fallback
    assert "╭" in out and "╰" in out  # rounded box borders
    assert "myhostname" in out
    assert "COMPRESSED" in out


def test_print_session_shows_metadata(tmp_path, capsys):
    sid = _insert_finished_session(tmp_path)
    db.print_session(sid)
    out = capsys.readouterr().out
    assert f"id: {sid}" in out
    assert "status: 0" in out


def test_print_session_pretty_mode_renders_a_boxed_table(tmp_path, monkeypatch):
    sid = _insert_finished_session(tmp_path, remote_host="myhostname")
    console = _force_terminal_console(monkeypatch)

    db.print_session(sid)

    out = console.file.getvalue()
    assert "\x1b[" in out
    assert "╭" in out and "╰" in out
    assert "myhostname" in out
    assert str(sid) in out


def test_print_session_not_found_exits(tmp_path):
    with pytest.raises(SystemExit):
        db.print_session(999999)


# --- grep_sessions -----------------------------------------------------------

def test_grep_finds_match_in_finished_session(tmp_path, capsys):
    sid = _insert_finished_session(tmp_path, content_lines=["hello world", "goodbye"])
    assert db.grep_sessions("hello") is True
    out = capsys.readouterr().out
    assert "hello world" in out
    assert f"[{sid}]" in out


def test_grep_no_match_reports_and_returns_false(tmp_path, capsys):
    _insert_finished_session(tmp_path, content_lines=["hello world"])
    assert db.grep_sessions("definitely_not_present_xyz") is False
    assert "no matches" in capsys.readouterr().err


def test_grep_unknown_session_id_exits(tmp_path):
    with pytest.raises(SystemExit):
        db.grep_sessions("x", session_id=99999)


def test_grep_regex_mode(tmp_path, capsys):
    _insert_finished_session(tmp_path, content_lines=["error: disk full", "info: ok"])
    assert db.grep_sessions(r"^error:", regex=True) is True
    assert "disk full" in capsys.readouterr().out


def test_grep_invalid_regex_exits(tmp_path):
    _insert_finished_session(tmp_path)
    with pytest.raises(SystemExit):
        db.grep_sessions("(unclosed", regex=True)


def test_grep_max_lines_truncates_and_notes_remainder(tmp_path, capsys):
    _insert_finished_session(tmp_path, content_lines=[f"match {i}" for i in range(10)])
    db.grep_sessions("match", max_lines=3)
    out = capsys.readouterr().out
    assert out.count("match ") == 3
    assert "7 more matches" in out


def test_grep_finished_session_missing_txt_member_exits(tmp_path):
    path = tmp_path / "c.rec"
    archive.create(path, "cast", pack_bytes(b"unused"))  # no "txt" member appended
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)
    with pytest.raises(SystemExit):
        db.grep_sessions("x", session_id=sid)


def test_grep_still_recording_renders_live_from_cast_file(tmp_path, capsys):
    events = [(0.0, "o", "livemarker text\r\n")]
    sid = _insert_running_session(tmp_path, events=events)
    assert db.grep_sessions("livemarker", session_id=sid) is True
    out = capsys.readouterr()
    assert "livemarker text" in out.out
    assert "still recording" in out.err


def test_grep_still_recording_included_in_whole_index_search(tmp_path, capsys):
    """A live session should also be picked up by a search across *all*
    sessions, not just when targeted by --session."""
    events = [(0.0, "o", "onlyinlivesession\r\n")]
    _insert_running_session(tmp_path, events=events)
    assert db.grep_sessions("onlyinlivesession") is True
    assert "onlyinlivesession" in capsys.readouterr().out


def test_grep_still_recording_nothing_flushed_yet_exits(tmp_path):
    """Neither the .rec archive nor the raw (uncompressed) file asciinema
    writes live to exists yet -- e.g. the row was just inserted, just
    before asciinema itself creates its output file."""
    path = tmp_path / "empty.rec"  # never created
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
    )
    with pytest.raises(SystemExit):
        db.grep_sessions("x", session_id=sid)


def test_grep_still_recording_raw_file_exists_but_empty_exits(tmp_path):
    """The raw file exists (asciinema created it) but nothing's been
    written to it yet -- still nothing to search."""
    path = tmp_path / "empty.rec"  # never created
    raw_cast_path(path).write_bytes(b"")
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
    )
    with pytest.raises(SystemExit):
        db.grep_sessions("x", session_id=sid)


def test_grep_still_recording_prefers_stale_archive_if_present(tmp_path, capsys):
    """Edge case: a .rec archive somehow already exists while the session
    still shows as running -- _live_transcript should read it rather than
    ignoring it or erroring."""
    events = [(0.0, "o", "stalecast marker\r\n")]
    sid = _insert_running_session_with_stale_archive(tmp_path, events=events)
    assert db.grep_sessions("stalecast", session_id=sid) is True
    assert "stalecast marker" in capsys.readouterr().out


def test_grep_result_includes_formatted_timestamp(tmp_path, capsys):
    _insert_finished_session(
        tmp_path, start="2026-01-01T10:00:00+00:00", content_lines=["needle here"],
    )
    db.grep_sessions("needle")
    out = capsys.readouterr().out
    assert "[2026-01-01 10:00:00]" in out


def test_grep_legacy_transcript_without_timestamps_still_matches(tmp_path, capsys):
    """A "txt" member rendered before timestamps were tracked has no tab
    prefix at all -- must still grep fine, showing '?' instead of a time."""
    path = tmp_path / "legacy.rec"
    archive.create(path, "cast", pack_bytes(b"unused"))
    archive.append(path, "txt", pack_bytes(b"an old-format line with no timestamp\n"))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)
    assert db.grep_sessions("old-format") is True
    out = capsys.readouterr().out
    assert "[?]" in out


def test_grep_finished_session_with_compression_disabled(tmp_path, capsys):
    """compressed=False sessions must be readable through the exact same
    read path -- unpack_bytes just skips the zstd step."""
    path = tmp_path / "raw.rec"
    archive.create(path, "cast", pack_bytes(b"unused", compress=False))
    archive.append(path, "txt", pack_bytes(b"0.0\tuncompressed needle\n", compress=False))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(), compressed=False,
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)
    assert db.grep_sessions("uncompressed needle") is True
    assert "uncompressed needle" in capsys.readouterr().out


# --- print_transcript ---------------------------------------------------------

def test_print_transcript_formats_timestamps(tmp_path, capsys):
    sid = _insert_finished_session(
        tmp_path, start="2026-01-01T10:00:00+00:00", content_lines=["first", "second"],
    )
    db.print_transcript(sid)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[2026-01-01 10:00:00] first"
    assert out[1] == "[2026-01-01 10:00:01] second"


def test_print_transcript_not_found_exits(tmp_path):
    with pytest.raises(SystemExit):
        db.print_transcript(424242)


def test_print_transcript_still_recording_warns_and_shows_partial(tmp_path, capsys):
    events = [(0.0, "o", "partial output\r\n")]
    sid = _insert_running_session(tmp_path, events=events)
    db.print_transcript(sid)
    captured = capsys.readouterr()
    assert "partial output" in captured.out
    assert "still recording" in captured.err


# --- encryption -----------------------------------------------------------
#
# Each encrypted member carries its own embedded salt (see crypto.py) -- no
# shared salt lives in config.json anymore, so sessions are free to use
# different passwords, and there's no external state whose loss could
# orphan anything already encrypted.

def test_grep_encrypted_finished_session_with_stored_password(tmp_path, capsys):
    password = _enable_encryption()
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["secret needle here"], password=password)
    assert db.grep_sessions("needle") is True
    out = capsys.readouterr().out
    assert "secret needle here" in out
    assert f"[{sid}]" in out


def test_grep_encrypted_session_wrong_stored_password_fails_clearly(tmp_path, capsys):
    config.set_encryption(enabled=True, password="totally-wrong", store_password=True)
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["secret needle here"], password="hunter2")
    with pytest.raises(SystemExit):
        db.grep_sessions("needle", session_id=sid)
    err = capsys.readouterr().err
    assert "that password didn't decrypt" in err
    # the wrong-password reason must be the one surfaced, not a misleading
    # "try again shortly, rendering in progress" that implies waiting helps
    assert "try again shortly" not in err


def test_grep_encrypted_session_targeted_without_any_password_exits(tmp_path):
    sid = _insert_finished_encrypted_session(tmp_path, password="hunter2")
    # encryption never configured in this test, and pytest's stdin isn't a
    # tty -- resolve_password() has nothing to offer and nothing to prompt
    with pytest.raises(SystemExit):
        db.grep_sessions("hello", session_id=sid)


def test_grep_encrypted_session_whole_index_search_skips_without_password(tmp_path, capsys):
    _insert_finished_encrypted_session(tmp_path, content_lines=["secret needle here"], password="hunter2")
    _insert_finished_session(tmp_path, name="plain", content_lines=["plain needle here"])
    # no encryption configured -- the encrypted session is silently
    # skipped (like a session with no ready transcript), the plain one
    # is still found
    assert db.grep_sessions("needle") is True
    out = capsys.readouterr().out
    assert "plain needle here" in out
    assert "secret needle here" not in out


def test_grep_still_running_encrypted_session_needs_no_password(tmp_path, capsys):
    """The key finding this test locks in: a session flagged `encrypted`
    that's still recording reads from the always-plaintext raw file
    asciinema itself writes -- it must NOT demand a password just because
    it's destined to be encrypted once finalized."""
    events = [(0.0, "o", "livesecret marker\r\n")]
    sid = _insert_running_encrypted_session(tmp_path, events=events)
    assert db.grep_sessions("livesecret", session_id=sid) is True
    assert "livesecret marker" in capsys.readouterr().out


def test_grep_encrypted_session_retries_with_a_different_password(tmp_path, monkeypatch, capsys):
    """The point of the whole redesign: sessions can each have their own
    password. A session encrypted under a password different from
    whatever's configured/stored must still be readable by typing the
    right one when asked, not just fail outright."""
    _enable_encryption("the-usual-password")
    sid = _insert_finished_encrypted_session(
        tmp_path, content_lines=["oddball needle"], password="a-completely-different-password",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-completely-different-password")
    assert db.grep_sessions("oddball", session_id=sid) is True
    assert "oddball needle" in capsys.readouterr().out


def test_cat_encrypted_session_with_stored_password(tmp_path, capsys):
    password = _enable_encryption()
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["first", "second"], password=password)
    db.print_transcript(sid)
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("first")
    assert out[1].endswith("second")


def test_cat_encrypted_session_without_password_exits(tmp_path):
    sid = _insert_finished_encrypted_session(tmp_path, password="hunter2")
    with pytest.raises(SystemExit):
        db.print_transcript(sid)


def test_cat_encrypted_session_wrong_stored_password_fails_clearly(tmp_path, capsys):
    config.set_encryption(enabled=True, password="totally-wrong", store_password=True)
    sid = _insert_finished_encrypted_session(tmp_path, password="hunter2")
    with pytest.raises(SystemExit):
        db.print_transcript(sid)
    err = capsys.readouterr().err
    assert "that password didn't decrypt" in err
    assert "try again shortly" not in err


def test_cat_still_running_encrypted_session_needs_no_password(tmp_path, capsys):
    events = [(0.0, "o", "livesecret marker\r\n")]
    sid = _insert_running_encrypted_session(tmp_path, events=events)
    db.print_transcript(sid)
    out = capsys.readouterr()
    assert "livesecret marker" in out.out
    assert "still recording" in out.err


# --- decrypt_session / encrypt_session -------------------------------------

def test_decrypt_session_not_found_exits():
    with pytest.raises(SystemExit):
        db.decrypt_session(999999)


def test_decrypt_session_not_encrypted_exits(tmp_path):
    sid = _insert_finished_session(tmp_path)
    with pytest.raises(SystemExit):
        db.decrypt_session(sid)


def test_decrypt_session_still_recording_exits(tmp_path):
    sid = _insert_running_encrypted_session(tmp_path, events=[(0.0, "o", "hi\r\n")])
    with pytest.raises(SystemExit):
        db.decrypt_session(sid)


def test_decrypt_session_without_password_exits(tmp_path):
    sid = _insert_finished_encrypted_session(tmp_path, password="hunter2")
    # encryption never configured in this test, pytest's stdin isn't a
    # tty -- nothing to try, nothing to prompt for
    with pytest.raises(SystemExit):
        db.decrypt_session(sid)


def test_decrypt_session_rewrites_archive_in_place_and_clears_flag(tmp_path, capsys):
    password = _enable_encryption()
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["secret line"], password=password)
    row_before = db.get_session(sid)
    path = Path(row_before["path"])
    assert path.exists()

    db.decrypt_session(sid)

    row_after = db.get_session(sid)
    assert row_after["encrypted"] == 0
    assert row_after["path"] == row_before["path"]  # no rename -- same archive, in place
    assert path.exists()

    # readable without any password now
    assert unpack_bytes(archive.read_member(path, "cast")) == b"unused"
    assert "secret line" in unpack_bytes(archive.read_member(path, "txt")).decode()
    assert "decrypted in place" in capsys.readouterr().out


def test_decrypt_session_wrong_password_fails_and_leaves_archive_untouched(tmp_path):
    config.set_encryption(enabled=True, password="totally-wrong", store_password=True)
    sid = _insert_finished_encrypted_session(tmp_path, password="hunter2")
    row = db.get_session(sid)
    path = Path(row["path"])
    cast_before = archive.read_member(path, "cast")

    with pytest.raises(SystemExit):
        db.decrypt_session(sid)

    row_after = db.get_session(sid)
    assert row_after["encrypted"] == 1
    assert row_after["path"] == row["path"]
    assert archive.read_member(path, "cast") == cast_before  # untouched


def test_decrypt_session_retries_with_a_different_password(tmp_path, monkeypatch):
    _enable_encryption("the-usual-password")
    sid = _insert_finished_encrypted_session(tmp_path, password="a-completely-different-password")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-completely-different-password")
    db.decrypt_session(sid)
    assert db.get_session(sid)["encrypted"] == 0


def test_decrypt_session_without_txt_member_yet_still_decrypts_cast(tmp_path):
    """A session can finish encrypted before its background transcript
    render catches up -- decrypting must not choke on the "txt" member
    that isn't there yet."""
    password = _enable_encryption()
    path = tmp_path / "notxt.rec"
    archive.create(path, "cast", pack_bytes(b"unused", password=password))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(), encrypted=True,
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)

    db.decrypt_session(sid)

    assert db.get_session(sid)["encrypted"] == 0
    assert unpack_bytes(archive.read_member(path, "cast")) == b"unused"
    assert archive.has_member(path, "txt") is False


def test_encrypt_session_not_found_exits():
    with pytest.raises(SystemExit):
        db.encrypt_session(999999)


def test_encrypt_session_already_encrypted_exits(tmp_path):
    password = _enable_encryption()
    sid = _insert_finished_encrypted_session(tmp_path, password=password)
    with pytest.raises(SystemExit):
        db.encrypt_session(sid)


def test_encrypt_session_still_recording_exits(tmp_path):
    sid = _insert_running_session(tmp_path, events=[(0.0, "o", "hi\r\n")])
    with pytest.raises(SystemExit):
        db.encrypt_session(sid)


def test_encrypt_session_without_password_configured_exits(tmp_path):
    sid = _insert_finished_session(tmp_path)
    with pytest.raises(SystemExit):
        db.encrypt_session(sid)


def test_encrypt_session_rewrites_archive_in_place_and_sets_flag(tmp_path, capsys):
    password = _enable_encryption()
    sid = _insert_finished_session(tmp_path, content_lines=["plain line"])
    row_before = db.get_session(sid)
    path = Path(row_before["path"])

    db.encrypt_session(sid)

    row_after = db.get_session(sid)
    assert row_after["encrypted"] == 1
    assert row_after["path"] == row_before["path"]  # no rename
    assert path.exists()

    assert unpack_bytes(archive.read_member(path, "cast"), password=password) == b"unused"
    assert "plain line" in unpack_bytes(archive.read_member(path, "txt"), password=password).decode()
    assert "encrypted in place" in capsys.readouterr().out


def test_encrypt_session_preserves_compression_setting(tmp_path):
    """Encrypting/decrypting only ever changes the write password -- a
    session recorded with compression off must stay uncompressed."""
    password = _enable_encryption()
    path = tmp_path / "raw.rec"
    archive.create(path, "cast", pack_bytes(b"unused", compress=False))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(), compressed=False,
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)

    db.encrypt_session(sid)

    assert db.get_session(sid)["compressed"] == 0
    assert unpack_bytes(archive.read_member(path, "cast"), compressed=False, password=password) == b"unused"


def test_decrypt_then_encrypt_roundtrip_preserves_content(tmp_path):
    password = _enable_encryption()
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["roundtrip line"], password=password)

    db.decrypt_session(sid)
    assert db.get_session(sid)["encrypted"] == 0
    assert db.grep_sessions("roundtrip", session_id=sid) is True  # readable with no password at all now

    db.encrypt_session(sid)
    assert db.get_session(sid)["encrypted"] == 1
    assert db.grep_sessions("roundtrip", session_id=sid) is True  # readable again with the (same, stored) password


def test_encrypt_then_decrypt_with_different_password_than_globally_configured(tmp_path):
    """encrypt_session always uses whatever's currently
    configured/resolvable -- re-encrypting under a different password than
    a session originally had is fine (each member is independent); decrypt
    just needs whichever password was actually used, prompted for if the
    configured one doesn't match."""
    _enable_encryption("first-password")
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["x"], password="first-password")
    db.decrypt_session(sid)

    config.set_encryption(enabled=True, password="second-password", store_password=True)
    db.encrypt_session(sid)
    assert db.get_session(sid)["encrypted"] == 1

    row = db.get_session(sid)
    cast = archive.read_member(Path(row["path"]), "cast")
    assert unpack_bytes(cast, password="second-password") == b"unused"


# --- delete_session / delete_all_sessions / all_session_ids ----------------

def test_delete_session_not_found_exits():
    with pytest.raises(SystemExit):
        db.delete_session(999999)


def test_delete_session_removes_archive_and_row(tmp_path):
    sid = _insert_finished_session(tmp_path, name="del1")
    path = Path(db.get_session(sid)["path"])
    assert path.exists()

    db.delete_session(sid)

    assert db.get_session(sid) is None
    assert not path.exists()


def test_delete_session_missing_txt_member_is_not_an_error(tmp_path):
    """A session can finish without ever getting a "txt" member (e.g. the
    background _render process never got to run) -- deleting it must not
    choke on the member that was never there."""
    path = tmp_path / "notxt.rec"
    archive.create(path, "cast", pack_bytes(b"unused"))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", path=str(path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)

    db.delete_session(sid)
    assert db.get_session(sid) is None


def test_delete_session_still_recording_with_live_pid_refused(tmp_path):
    sid = _insert_running_session(tmp_path, name="live")
    with pytest.raises(SystemExit):
        db.delete_session(sid)
    assert db.get_session(sid) is not None  # nothing removed


def test_delete_session_still_recording_with_live_pid_force_deletes_anyway(tmp_path):
    sid = _insert_running_session(tmp_path, name="live2")
    row = db.get_session(sid)
    raw_path = raw_cast_path(Path(row["path"]))
    assert raw_path.exists()

    db.delete_session(sid, force=True)

    assert db.get_session(sid) is None
    assert not raw_path.exists()


def test_delete_session_orphaned_dead_pid_deletes_without_force(tmp_path):
    """A session whose recorder process already died (`KILLED?` in `korec
    ls`) -- an orphaned recording -- is fine to delete without --force,
    since nothing is actively writing to its files anymore."""
    path = tmp_path / "orphan.rec"
    raw_cast_path(path).write_bytes(_make_cast_bytes([]))
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="local", remote_host="remotehost",
        tty="pts_9", path=str(path),
        pid=2**30,  # implausible -- see test_pid_alive_false_for_implausible_pid
    )

    db.delete_session(sid)

    assert db.get_session(sid) is None
    assert not raw_cast_path(path).exists()


def test_all_session_ids_empty_when_no_sessions():
    assert db.all_session_ids() == []


def test_all_session_ids_returns_every_id_in_order(tmp_path):
    ids = [_insert_finished_session(tmp_path, name=f"ord{i}") for i in range(3)]
    assert db.all_session_ids() == sorted(ids)


def test_delete_all_sessions_removes_everything_and_all_files(tmp_path):
    sids = [_insert_finished_session(tmp_path, name=f"all{i}") for i in range(3)]
    paths = [Path(db.get_session(sid)["path"]) for sid in sids]

    deleted, skipped = db.delete_all_sessions()

    assert deleted == 3
    assert skipped == 0
    assert db.all_session_ids() == []
    for p in paths:
        assert not p.exists()


def test_delete_all_sessions_skips_still_recording_without_force(tmp_path):
    finished_sid = _insert_finished_session(tmp_path, name="fin")
    running_sid = _insert_running_session(tmp_path, name="run")

    deleted, skipped = db.delete_all_sessions()

    assert deleted == 1
    assert skipped == 1
    assert db.get_session(finished_sid) is None
    assert db.get_session(running_sid) is not None


def test_delete_all_sessions_force_deletes_running_sessions_too(tmp_path):
    running_sid = _insert_running_session(tmp_path, name="run2")

    deleted, skipped = db.delete_all_sessions(force=True)

    assert deleted == 1
    assert skipped == 0
    assert db.get_session(running_sid) is None


def test_delete_all_sessions_empty_index_is_a_noop():
    assert db.delete_all_sessions() == (0, 0)


def test_delete_all_sessions_resets_id_sequence_when_fully_empty(tmp_path):
    for i in range(3):
        _insert_finished_session(tmp_path, name=f"seq{i}")

    db.delete_all_sessions()

    new_sid = _insert_finished_session(tmp_path, name="after")
    assert new_sid == 1


def test_delete_all_sessions_does_not_reset_sequence_when_something_skipped(tmp_path):
    """A resurrected id would collide with the still-recording session
    that was deliberately left in place."""
    _insert_finished_session(tmp_path, name="fin")
    running_sid = _insert_running_session(tmp_path, name="run")

    deleted, skipped = db.delete_all_sessions()
    assert deleted == 1 and skipped == 1

    new_sid = _insert_finished_session(tmp_path, name="after")
    assert new_sid > running_sid
