from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from korecord import config, db
from korecord.compress import compress_bytes_to_file, decompress_file


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
    """A session with a ready .txt sidecar, as if `_render` already ran."""
    cast_path = tmp_path / f"{name}.cast.zst"
    txt_path = tmp_path / f"{name}.txt.zst"
    compress_bytes_to_file(b"unused", cast_path)
    txt = "".join(f"{float(i)}\t{line}\n" for i, line in enumerate(content_lines))
    compress_bytes_to_file(txt.encode(), txt_path)
    sid = db.insert_pending_session(
        start=start, local_host="local", remote_host=remote_host,
        tty="pts_1", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:05:00+00:00", duration=300, cast_size=10, exit_code=exit_code)
    return sid


def _insert_running_session(tmp_path, *, name="r", start="2026-01-01T10:00:00+00:00", events=()):
    """A session still recording: end_time is NULL, no .txt sidecar built
    yet, and no .cast.zst either -- asciinema (3.x) writes straight to the
    plain, uncompressed file at the "raw" path (cast_path with ".zst"
    stripped) until the session ends and `record()` compresses it. That's
    the only place data actually exists while a session is live."""
    cast_path = tmp_path / f"{name}.cast.zst"  # deliberately never created
    raw_path = tmp_path / f"{name}.cast"
    raw_path.write_bytes(_make_cast_bytes(events))
    txt_path = tmp_path / f"{name}.txt.zst"  # deliberately never created
    return db.insert_pending_session(
        start=start, local_host="local", remote_host="remotehost",
        tty="pts_2", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
    )


def _insert_running_session_with_stale_compressed_cast(tmp_path, *, name="rc", events=()):
    """Edge case: a .cast.zst already exists (e.g. left over) while the
    session still shows as running -- _live_transcript should prefer it
    over the raw file rather than erroring or ignoring it."""
    cast_path = tmp_path / f"{name}.cast.zst"
    compress_bytes_to_file(_make_cast_bytes(events), cast_path)
    txt_path = tmp_path / f"{name}.txt.zst"  # deliberately never created
    return db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="local", remote_host="remotehost",
        tty="pts_3", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
    )


def _insert_finished_encrypted_session(
    tmp_path, *, name="enc", start="2026-01-01T10:00:00+00:00", content_lines=("hello world",), password,
):
    """A finished, encrypted session -- both .cast.zst.enc and .txt.zst.enc
    written with `password`, matching how record()/_render actually
    produce them (and name them) when encryption is on. Each file gets its
    own independently-generated salt embedded in it (see crypto.py), even
    though both use the same password here."""
    cast_path = tmp_path / f"{name}.cast.zst.enc"
    txt_path = tmp_path / f"{name}.txt.zst.enc"
    compress_bytes_to_file(b"unused", cast_path, password=password)
    txt = "".join(f"{float(i)}\t{line}\n" for i, line in enumerate(content_lines))
    compress_bytes_to_file(txt.encode(), txt_path, password=password)
    sid = db.insert_pending_session(
        start=start, local_host="local", remote_host="remotehost",
        tty="pts_5", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
        encrypted=True,
    )
    db.finalize_session(sid, end="2026-01-01T10:05:00+00:00", duration=300, cast_size=10, exit_code=0)
    return sid


def _insert_running_encrypted_session(tmp_path, *, name="encr", events=()):
    """A still-recording session flagged `encrypted` -- but its raw file
    (what asciinema actually writes live) is always plaintext regardless,
    since asciinema has no idea korec encrypts the finished artifact. Uses
    the real ".cast.zst.enc" naming record() gives encrypted sessions."""
    cast_path = tmp_path / f"{name}.cast.zst.enc"  # deliberately never created
    raw_path = tmp_path / f"{name}.cast"
    raw_path.write_bytes(_make_cast_bytes(events))
    txt_path = tmp_path / f"{name}.txt.zst.enc"  # deliberately never created
    return db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="local", remote_host="remotehost",
        tty="pts_6", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
        encrypted=True,
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
        tty="pts_5", cast_path=str(tmp_path / "x.cast.zst"), txt_path=str(tmp_path / "x.txt.zst"),
        pid=os.getpid(),
    )
    row = db.get_session(sid)
    assert row["end_time"] is None
    assert row["remote_host"] == "rh"

    db.finalize_session(sid, end="2026-01-01T10:10:00+00:00", duration=600, cast_size=123, exit_code=0)
    row = db.get_session(sid)
    assert row["end_time"] == "2026-01-01T10:10:00+00:00"
    assert row["exit_code"] == 0
    assert row["cast_size"] == 123


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


def test_print_session_shows_metadata(tmp_path, capsys):
    sid = _insert_finished_session(tmp_path)
    db.print_session(sid)
    out = capsys.readouterr().out
    assert f"id: {sid}" in out
    assert "status: 0" in out


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


def test_grep_finished_session_missing_transcript_file_exits(tmp_path):
    cast_path = tmp_path / "c.cast.zst"
    compress_bytes_to_file(b"unused", cast_path)
    txt_path = tmp_path / "missing.txt.zst"  # never created
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
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
    """Neither the compressed .cast.zst nor the raw (uncompressed) file
    asciinema writes live to exists yet -- e.g. the row was just inserted,
    just before asciinema itself creates its output file."""
    cast_path = tmp_path / "empty.cast.zst"  # never created
    txt_path = tmp_path / "empty.txt.zst"
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
    )
    with pytest.raises(SystemExit):
        db.grep_sessions("x", session_id=sid)


def test_grep_still_recording_raw_file_exists_but_empty_exits(tmp_path):
    """The raw file exists (asciinema created it) but nothing's been
    written to it yet -- still nothing to search."""
    cast_path = tmp_path / "empty.cast.zst"  # never created
    (tmp_path / "empty.cast").write_bytes(b"")
    txt_path = tmp_path / "empty.txt.zst"
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
    )
    with pytest.raises(SystemExit):
        db.grep_sessions("x", session_id=sid)


def test_grep_still_recording_prefers_compressed_cast_if_present(tmp_path, capsys):
    """Edge case: a .cast.zst somehow already exists while the session
    still shows as running -- _live_transcript should read it rather than
    ignoring it or erroring."""
    events = [(0.0, "o", "stalecast marker\r\n")]
    sid = _insert_running_session_with_stale_compressed_cast(tmp_path, events=events)
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
    """A .txt sidecar rendered before timestamps were tracked has no tab
    prefix at all -- must still grep fine, showing '?' instead of a time."""
    cast_path = tmp_path / "legacy.cast.zst"
    txt_path = tmp_path / "legacy.txt.zst"
    compress_bytes_to_file(b"unused", cast_path)
    compress_bytes_to_file(b"an old-format line with no timestamp\n", txt_path)
    sid = db.insert_pending_session(
        start="2026-01-01T10:00:00+00:00", local_host="l", remote_host="r",
        tty="t", cast_path=str(cast_path), txt_path=str(txt_path), pid=os.getpid(),
    )
    db.finalize_session(sid, end="2026-01-01T10:01:00+00:00", duration=60, cast_size=1, exit_code=0)
    assert db.grep_sessions("old-format") is True
    out = capsys.readouterr().out
    assert "[?]" in out


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
# Each encrypted file carries its own embedded salt (see crypto.py) -- no
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


def test_decrypt_session_rewrites_files_and_clears_flag(tmp_path, capsys):
    password = _enable_encryption()
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["secret line"], password=password)
    row_before = db.get_session(sid)
    old_cast, old_txt = Path(row_before["cast_path"]), Path(row_before["txt_path"])
    assert old_cast.exists() and old_txt.exists()

    db.decrypt_session(sid)

    row_after = db.get_session(sid)
    assert row_after["encrypted"] == 0
    new_cast, new_txt = Path(row_after["cast_path"]), Path(row_after["txt_path"])
    assert str(new_cast) == str(old_cast)[: -len(".enc")]
    assert str(new_txt) == str(old_txt)[: -len(".enc")]
    assert not old_cast.exists()
    assert not old_txt.exists()
    assert new_cast.exists()
    assert new_txt.exists()

    # readable without any password now
    assert decompress_file(new_cast) == b"unused"
    assert "secret line" in decompress_file(new_txt).decode()
    assert "decrypted in place" in capsys.readouterr().out


def test_decrypt_session_wrong_password_fails_and_leaves_files_untouched(tmp_path):
    config.set_encryption(enabled=True, password="totally-wrong", store_password=True)
    sid = _insert_finished_encrypted_session(tmp_path, password="hunter2")
    row = db.get_session(sid)
    old_cast_path = row["cast_path"]

    with pytest.raises(SystemExit):
        db.decrypt_session(sid)

    row_after = db.get_session(sid)
    assert row_after["encrypted"] == 1
    assert row_after["cast_path"] == old_cast_path
    assert Path(old_cast_path).exists()


def test_decrypt_session_retries_with_a_different_password(tmp_path, monkeypatch):
    _enable_encryption("the-usual-password")
    sid = _insert_finished_encrypted_session(tmp_path, password="a-completely-different-password")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-completely-different-password")
    db.decrypt_session(sid)
    assert db.get_session(sid)["encrypted"] == 0


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


def test_encrypt_session_rewrites_files_and_sets_flag(tmp_path, capsys):
    password = _enable_encryption()
    sid = _insert_finished_session(tmp_path, content_lines=["plain line"])
    row_before = db.get_session(sid)
    old_cast, old_txt = Path(row_before["cast_path"]), Path(row_before["txt_path"])

    db.encrypt_session(sid)

    row_after = db.get_session(sid)
    assert row_after["encrypted"] == 1
    new_cast, new_txt = Path(row_after["cast_path"]), Path(row_after["txt_path"])
    assert new_cast == old_cast.with_name(old_cast.name + ".enc")
    assert new_txt == old_txt.with_name(old_txt.name + ".enc")
    assert not old_cast.exists()
    assert not old_txt.exists()

    assert decompress_file(new_cast, password=password) == b"unused"
    assert "plain line" in decompress_file(new_txt, password=password).decode()
    assert "encrypted in place" in capsys.readouterr().out


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
    a session originally had is fine (each file is independent); decrypt
    just needs whichever password was actually used, prompted for if the
    configured one doesn't match."""
    _enable_encryption("first-password")
    sid = _insert_finished_encrypted_session(tmp_path, content_lines=["x"], password="first-password")
    db.decrypt_session(sid)

    config.set_encryption(enabled=True, password="second-password", store_password=True)
    db.encrypt_session(sid)
    assert db.get_session(sid)["encrypted"] == 1

    row = db.get_session(sid)
    assert decompress_file(Path(row["cast_path"]), password="second-password") == b"unused"
