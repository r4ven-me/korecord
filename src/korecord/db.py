"""SQLite index of recorded sessions: one row per recording, pointing at
its `.rec` archive -- a tar container (see archive.py) holding the
asciicast recording and, once background rendering finishes, its
searchable plaintext transcript.

A row is inserted as soon as recording *starts* (end_time/exit_code/etc.
left NULL) and filled in when it ends, so a session is visible in `ls`
while it's still running, and a session killed mid-recording still leaves
a row behind instead of vanishing entirely -- its liveness is then
inferred from whether its recorded pid is still alive."""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rich import box
from rich.table import Table
from rich.text import Text

from . import archive, config, ui
from .compress import pack_bytes, unpack_bytes, unpack_bytes_with_retry

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration_sec INTEGER,
    local_host TEXT,
    remote_host TEXT,
    tty TEXT,
    path TEXT NOT NULL,
    cast_size INTEGER,
    exit_code INTEGER,
    pid INTEGER,
    encrypted INTEGER NOT NULL DEFAULT 0,
    compressed INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_remote_host ON sessions(remote_host);
"""


def _connect() -> sqlite3.Connection:
    p = config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    return conn


def insert_pending_session(
    *,
    start: str,
    local_host: str | None,
    remote_host: str | None,
    tty: str | None,
    path: str,
    pid: int,
    encrypted: bool = False,
    compressed: bool = True,
) -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO sessions
           (start_time, local_host, remote_host, tty, path, pid, encrypted, compressed)
           VALUES (?,?,?,?,?,?,?,?)""",
        (start, local_host, remote_host, tty, path, pid, int(encrypted), int(compressed)),
    )
    conn.commit()
    return cur.lastrowid


def finalize_session(
    session_id: int,
    *,
    end: str,
    duration: int,
    cast_size: int,
    exit_code: int | None,
) -> None:
    conn = _connect()
    conn.execute(
        """UPDATE sessions SET end_time=?, duration_sec=?, cast_size=?, exit_code=?
           WHERE id=?""",
        (end, duration, cast_size, exit_code, session_id),
    )
    conn.commit()


def _set_session_encrypted(session_id: int, *, encrypted: bool) -> None:
    """Used by decrypt_session/encrypt_session after rewriting a session's
    `.rec` archive in place -- updates the index to match, so `korec ls`'s
    ENC column and every read path (which decides whether to even try
    resolving a key from `encrypted`, not from anything about the
    filename -- there's nothing to sniff, every session is just
    `<base>.rec`) stay in sync with what's really on disk."""
    conn = _connect()
    conn.execute("UPDATE sessions SET encrypted=? WHERE id=?", (int(encrypted), session_id))
    conn.commit()


def get_session(session_id: int) -> sqlite3.Row | None:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def _human_size(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    else:
        return True


def _status(end_time: str | None, exit_code: int | None, pid: int | None) -> str:
    if end_time is not None:
        return str(exit_code) if exit_code is not None else "?"
    return "RUNNING" if _pid_alive(pid) else "KILLED?"


def _status_style(status: str) -> str:
    if status == "RUNNING":
        return "yellow"
    if status == "KILLED?":
        return "bold red"
    if status == "0":
        return "green"
    if status == "?":
        return "dim"
    return "red"  # a nonzero exit code, or a negative one -- killed by a signal


def print_sessions(*, limit: int = 50, host: str | None = None, since: str | None = None) -> None:
    conn = _connect()
    where, params = [], []
    if host:
        where.append("remote_host LIKE ?")
        params.append(f"%{host}%")
    if since:
        where.append("start_time >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""SELECT id, start_time, duration_sec, remote_host, tty, cast_size, exit_code, end_time, pid, encrypted, compressed
            FROM sessions {clause} ORDER BY start_time DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()

    if not ui.console.is_terminal:
        # Piped/redirected/captured -- a script (or a test) parsing this
        # output shouldn't have to deal with ANSI colors or box-drawing
        # characters, and shouldn't see output shaped differently than it
        # always has: same fixed-width, whitespace-separated columns as
        # before, just with COMPRESSED appended at the end.
        _print_sessions_plain(rows)
        return

    table = Table(box=box.ROUNDED, header_style="bold", border_style="grey50")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("START")
    table.add_column("DUR(s)", justify="right")
    table.add_column("HOST", style="bold magenta")
    table.add_column("TTY", style="dim")
    table.add_column("SIZE", justify="right")
    table.add_column("STATUS", justify="right")
    table.add_column("ENC", justify="center")
    table.add_column("COMPRESSED", justify="center")

    for id_, start, dur, rhost, tty, size, rc, end_time, pid, encrypted, compressed in rows:
        status = _status(end_time, rc, pid)
        table.add_row(
            str(id_), start or "", str(dur) if dur is not None else "-", rhost or "", tty or "",
            _human_size(size),
            Text(status, style=_status_style(status)),
            Text("✓", style="green") if encrypted else Text("-", style="dim"),
            Text("✓", style="cyan") if compressed else Text("-", style="dim"),
        )
    ui.console.print(table)


def _print_sessions_plain(rows) -> None:
    fmt = "{:<5} {:<26} {:>7} {:<20} {:<12} {:>7} {:>8} {:>3} {:>10}"
    print(fmt.format("ID", "START", "DUR(s)", "HOST", "TTY", "SIZE", "STATUS", "ENC", "COMPRESSED"))
    for id_, start, dur, rhost, tty, size, rc, end_time, pid, encrypted, compressed in rows:
        dur_display = dur if dur is not None else "-"
        print(fmt.format(id_, start or "", dur_display, rhost or "", tty or "",
                          _human_size(size), _status(end_time, rc, pid),
                          "*" if encrypted else "", "*" if compressed else ""))


def print_session(session_id: int) -> None:
    row = get_session(session_id)
    if row is None:
        sys.exit(f"korec: session {session_id} not found")
    status = _status(row["end_time"], row["exit_code"], row["pid"])

    if not ui.console.is_terminal:
        for key in row.keys():
            print(f"{key}: {row[key]}")
        print(f"status: {status}")
        return

    def value(key: str):
        if key in ("encrypted", "compressed"):
            return Text("✓", style="green" if key == "encrypted" else "cyan") if row[key] else Text("-", style="dim")
        return str(row[key])

    kv = [(key, value(key)) for key in row.keys()]
    kv.append(("status", Text(status, style=_status_style(status))))
    ui.print_kv_table(kv)


def _parse_transcript_line(line: str) -> tuple[float | None, str]:
    """Split a rendered transcript line back into (elapsed_seconds, content).
    Every line render_raw_cast produces has a leading `<seconds>\\t`
    prefix; this tolerates one that doesn't (missing tab, or a head that
    isn't a valid float) by returning elapsed=None rather than raising --
    a single malformed line shouldn't break `grep`/`cat` on the rest of
    the transcript."""
    head, sep, rest = line.partition("\t")
    if not sep:
        return None, line
    try:
        return float(head), rest
    except ValueError:
        return None, line


def _format_ts(start_iso: str, elapsed: float | None) -> str:
    if elapsed is None:
        return "?"
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError:
        return "?"
    return (start + timedelta(seconds=elapsed)).strftime("%Y-%m-%d %H:%M:%S")


def _live_transcript(row: sqlite3.Row) -> str | None:
    """Render whatever a still-recording session has captured so far,
    instead of the rendered "txt" archive member -- which only gets built
    (and appended, see archive.py) once the session ends. Mirrors what the
    background `_render` step does, just synchronously and against a
    possibly mid-line stream.

    A running session has no `.rec` archive yet -- asciinema writes
    straight to the plain, uncompressed file at
    `recorder.raw_cast_path(path)`, which only gets packed into the "cast"
    member of `path` once `record()` finishes (see recorder.py). Read that
    instead when the archive isn't there yet -- note it's always
    plaintext, regardless of the session's `compressed`/`encrypted` flags,
    since asciinema itself writes it and has no idea korec might transform
    the final artifact.

    Returns None if nothing usable has been flushed to disk yet."""
    from .recorder import raw_cast_path, sanitize_cast_data
    from .render import render_raw_cast

    path = row["path"]
    p = Path(path)
    try:
        if p.exists():
            blob = archive.read_member(p, "cast")
            if row["encrypted"]:
                raw = unpack_bytes_with_retry(
                    blob, compressed=row["compressed"], password=config.resolve_password(),
                    label=f"session {row['id']}",
                )
                if raw is None:
                    return None
            else:
                raw = unpack_bytes(blob, compressed=row["compressed"])
        else:
            raw_p = raw_cast_path(path)
            if not raw_p.exists():
                return None
            raw = raw_p.read_bytes()
        data = sanitize_cast_data(raw)
        if data is None:
            return None
        return render_raw_cast(data.decode("utf-8", "replace"))
    except Exception as e:
        print(f"korec: couldn't render live transcript for {path}: {e}", file=sys.stderr)
        return None


def _session_text(row: sqlite3.Row) -> str | None:
    """This session's rendered transcript (tab-timestamped, one line per
    output line) -- live-rendered from the cast member if it's still
    recording, otherwise read from the "txt" archive member. Prompts for
    (and retries) a password itself if the session is encrypted -- see
    compress.py's unpack_bytes_with_retry. None if nothing's available yet
    either way (including a finished session whose background render
    hasn't caught up -- no "txt" member yet), or decryption never
    succeeds."""
    if row["end_time"] is None:
        return _live_transcript(row)
    path = Path(row["path"])
    if not path.exists():
        return None
    blob = archive.read_member(path, "txt")
    if blob is None:
        return None
    if not row["encrypted"]:
        try:
            return unpack_bytes(blob, compressed=row["compressed"]).decode("utf-8", "ignore")
        except Exception:
            return None
    raw = unpack_bytes_with_retry(
        blob, compressed=row["compressed"], password=config.resolve_password(), label=f"session {row['id']}",
    )
    return raw.decode("utf-8", "ignore") if raw is not None else None


def grep_sessions(pattern: str, *, regex: bool = False, max_lines: int = 5, session_id: int | None = None) -> bool:
    """Returns True iff at least one session matched (like grep's exit code).

    If session_id is given, search only that session -- and say clearly why
    there's nothing to search if its transcript isn't ready yet, rather than
    silently reporting "no matches" the way a whole-index search would.

    A session still being recorded has no rendered .txt sidecar yet, so its
    transcript is rendered live from the cast file instead, covering
    everything flushed so far -- see `_live_transcript`.

    Each matched line is printed with the wall-clock timestamp it appeared
    at (start_time + the line's recording-relative offset) -- feed that,
    or the session id, to `korec cat <id> | less` to read the surrounding
    context."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    if session_id is not None:
        row = get_session(session_id)
        if row is None:
            sys.exit(f"korec: session {session_id} not found")
        rows = [row]
    else:
        rows = conn.execute("SELECT * FROM sessions ORDER BY start_time DESC").fetchall()

    try:
        pat = re.compile(pattern) if regex else None
    except re.error as e:
        sys.exit(f"korec: invalid regex {pattern!r}: {e}")

    any_hit = False
    for row in rows:
        id_, start, rhost = row["id"], row["start_time"], row["remote_host"]
        live = row["end_time"] is None
        text = _session_text(row)
        if text is None:
            if session_id is not None:
                if row["encrypted"] and not live:
                    # _session_text (via decrypt_file_with_retry) already
                    # printed the specific reason to stderr -- don't
                    # follow it with a misleading "try again shortly" as
                    # if waiting would help.
                    sys.exit(f"korec: couldn't read session {session_id} (see error above)")
                reason = (
                    "it's still recording, try again shortly" if live else
                    "background rendering may still be in progress, try again shortly"
                )
                sys.exit(f"korec: nothing searchable for session {session_id} yet -- {reason}")
            continue
        if live and session_id is not None:
            print(
                f"korec: session {session_id} is still recording -- "
                "searching what's been captured so far",
                file=sys.stderr,
            )
        source = row["path"]

        matches = []
        for raw_line in text.splitlines():
            elapsed, content = _parse_transcript_line(raw_line)
            if pat.search(content) if pat else pattern in content:
                matches.append((elapsed, content))
        if matches:
            any_hit = True
            print(f"=== [{id_}] {start} {rhost} ({source}) ===")
            for elapsed, content in matches[:max_lines]:
                print(f"  [{_format_ts(start, elapsed)}] {content.strip()}")
            if len(matches) > max_lines:
                print(f"  ... ({len(matches) - max_lines} more matches)")
    if not any_hit:
        print("no matches", file=sys.stderr)
    return any_hit


def print_transcript(session_id: int) -> None:
    """Print a session's full transcript to stdout, one `[timestamp] line`
    per line -- meant for `korec cat <id> | less`. Pair with `korec grep`:
    grep across sessions to find the session id and rough time, then cat
    that session and jump to it in less (`/<time>` or `/<text>`) to read
    the surrounding context."""
    row = get_session(session_id)
    if row is None:
        sys.exit(f"korec: session {session_id} not found")
    if row["end_time"] is None:
        print(
            f"korec: session {session_id} is still recording -- "
            "showing what's been captured so far",
            file=sys.stderr,
        )
    text = _session_text(row)
    if text is None:
        if row["encrypted"] and row["end_time"] is not None:
            sys.exit(f"korec: couldn't read session {session_id} (see error above)")
        sys.exit(f"korec: nothing to show for session {session_id} yet -- try again shortly")
    start = row["start_time"]
    for raw_line in text.splitlines():
        elapsed, content = _parse_transcript_line(raw_line)
        print(f"[{_format_ts(start, elapsed)}] {content}")


def _rewrite_session_archive(session_id: int, row: sqlite3.Row, *, write_password: str | None) -> None:
    """Shared machinery for decrypt_session/encrypt_session: reads each
    member of a session's `.rec` archive -- decrypting, with an
    interactive retry-on-wrong-password (see compress.py's
    unpack_bytes_with_retry), if `row["encrypted"]`; reading as plain
    otherwise. Note this is NOT the same thing as "a password happens to
    be resolvable" -- a session that genuinely has no password available
    yet still needs the encrypted read path (so unpack_bytes_with_retry
    gets a chance to prompt), while a session that isn't encrypted at all
    must never be run through decryption just because some password
    happens to be lying around.

    Rebuilds the archive from scratch at the same path -- encrypting/
    decrypting a session never renames anything, just changes what's
    inside. Compression (`row["compressed"]`) is untouched either way;
    only the write password changes. Only rewrites a "txt" member if one
    already exists -- a session's transcript may not be built yet."""
    path = Path(row["path"])
    read_password = config.resolve_password() if row["encrypted"] else None

    def read(name: str) -> bytes:
        blob = archive.read_member(path, name)
        if not row["encrypted"]:
            return unpack_bytes(blob, compressed=row["compressed"])
        raw = unpack_bytes_with_retry(
            blob, compressed=row["compressed"], password=read_password, label=f"session {session_id}",
        )
        if raw is None:
            sys.exit(f"korec: giving up on session {session_id} -- couldn't decrypt {path}")
        return raw

    def pack(data: bytes) -> bytes:
        return pack_bytes(data, compress=row["compressed"], password=write_password)

    has_txt = archive.has_member(path, "txt")
    new_cast = pack(read("cast"))
    new_txt = pack(read("txt")) if has_txt else None

    archive.create(path, "cast", new_cast)
    if new_txt is not None:
        archive.append(path, "txt", new_txt)

    _set_session_encrypted(session_id, encrypted=write_password is not None)


def decrypt_session(session_id: int) -> None:
    """Permanently decrypts an encrypted session's `.rec` archive in
    place, and clears the session's `encrypted` flag in the index -- no
    password needed to read it, ever again. There's no undo built into
    this specific direction; re-encrypt with `encrypt_session` if you
    want it back."""
    row = get_session(session_id)
    if row is None:
        sys.exit(f"korec: session {session_id} not found")
    if not row["encrypted"]:
        sys.exit(f"korec: session {session_id} isn't encrypted -- nothing to do")
    if row["end_time"] is None:
        sys.exit(
            f"korec: session {session_id} is still recording -- there's nothing encrypted "
            "on disk yet to decrypt (it'll be encrypted once the session finishes)"
        )

    _rewrite_session_archive(session_id, row, write_password=None)
    print(f"korec: session {session_id} decrypted in place -- no password needed for it anymore")


def _delete_session_files(row: sqlite3.Row) -> None:
    """Removes every file on disk for one session: its `.rec` archive,
    plus -- for a session that never finished -- the plain raw file
    asciinema was writing to (see recorder.raw_cast_path). Best-effort: a
    file that's already gone (partial prior cleanup) is not an error."""
    from .recorder import raw_cast_path

    Path(row["path"]).unlink(missing_ok=True)
    raw_cast_path(row["path"]).unlink(missing_ok=True)


def delete_session(session_id: int, *, force: bool = False) -> None:
    """Permanently deletes one session: its files (see
    _delete_session_files) and its row in the index. Refuses a session
    that's still actively recording (its pid is alive) unless
    `force=True` -- deleting its files out from under a live asciinema
    process would corrupt whatever it's still writing. A session whose
    recorder already died (`KILLED?` in `korec ls`) is fair game either
    way -- that's exactly the kind of orphaned recording this is meant to
    clean up (see recorder.py's SIGTERM/SIGHUP handling)."""
    row = get_session(session_id)
    if row is None:
        sys.exit(f"korec: session {session_id} not found")
    if row["end_time"] is None and _pid_alive(row["pid"]) and not force:
        sys.exit(
            f"korec: session {session_id} is still recording (pid {row['pid']} alive) -- "
            "stop it first, or pass --force to delete it anyway"
        )

    _delete_session_files(row)
    conn = _connect()
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()


def all_session_ids() -> list[int]:
    conn = _connect()
    return [r[0] for r in conn.execute("SELECT id FROM sessions ORDER BY id")]


def delete_all_sessions(*, force: bool = False) -> tuple[int, int]:
    """Deletes every session's files and row, skipping (unless
    `force=True`) ones still actively recording. Returns
    (deleted_count, skipped_running_count). Confirmation is the CLI
    layer's job (close to the user's terminal), not this function's.

    Also resets the `id` autoincrement counter, but only when the table
    ends up completely empty (nothing was skipped) -- `id` is
    `INTEGER PRIMARY KEY AUTOINCREMENT`, so SQLite otherwise keeps handing
    out ever-higher ids forever even once every row is gone. Resetting it
    when a skipped (still-recording) session survives would risk a future
    insert reusing that session's own id."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM sessions").fetchall()

    deleted_ids = []
    skipped = 0
    for row in rows:
        if row["end_time"] is None and _pid_alive(row["pid"]) and not force:
            skipped += 1
            continue
        _delete_session_files(row)
        deleted_ids.append(row["id"])

    if deleted_ids:
        placeholders = ",".join("?" * len(deleted_ids))
        conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", deleted_ids)
        if not skipped:
            conn.execute("DELETE FROM sqlite_sequence WHERE name='sessions'")
        conn.commit()
    return len(deleted_ids), skipped


def encrypt_session(session_id: int) -> None:
    """The mirror of decrypt_session: encrypts a currently-plain session's
    `.rec` archive in place with the configured password, and sets the
    session's `encrypted` flag."""
    row = get_session(session_id)
    if row is None:
        sys.exit(f"korec: session {session_id} not found")
    if row["encrypted"]:
        sys.exit(f"korec: session {session_id} is already encrypted -- nothing to do")
    if row["end_time"] is None:
        sys.exit(f"korec: session {session_id} is still recording -- nothing to encrypt yet")
    password = config.resolve_password()
    if password is None:
        sys.exit(
            "korec: no encryption password available -- set $KORECORD_PASSWORD, run "
            "`korec config encryption enable` to store one, or run this interactively "
            "so korec can prompt"
        )

    _rewrite_session_archive(session_id, row, write_password=password)
    print(f"korec: session {session_id} encrypted in place")
