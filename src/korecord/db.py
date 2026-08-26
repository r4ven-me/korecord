"""SQLite index of recorded sessions: one row per recording, pointing at
its compressed asciicast and (once background rendering finishes) its
searchable plaintext sidecar.

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
from typing import Callable

from . import config
from .compress import compress_bytes_to_file, decompress_file, decrypt_file_with_retry

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration_sec INTEGER,
    local_host TEXT,
    remote_host TEXT,
    tty TEXT,
    cast_path TEXT NOT NULL,
    txt_path TEXT,
    cast_size INTEGER,
    exit_code INTEGER,
    pid INTEGER,
    encrypted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_remote_host ON sessions(remote_host);
"""


def _connect() -> sqlite3.Connection:
    p = config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "pid" not in existing_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN pid INTEGER")
        conn.commit()
    if "encrypted" not in existing_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    return conn


def insert_pending_session(
    *,
    start: str,
    local_host: str | None,
    remote_host: str | None,
    tty: str | None,
    cast_path: str,
    txt_path: str | None,
    pid: int,
    encrypted: bool = False,
) -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO sessions
           (start_time, local_host, remote_host, tty, cast_path, txt_path, pid, encrypted)
           VALUES (?,?,?,?,?,?,?,?)""",
        (start, local_host, remote_host, tty, cast_path, txt_path, pid, int(encrypted)),
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


def _set_session_crypto_state(session_id: int, *, encrypted: bool, cast_path: str, txt_path: str | None) -> None:
    """Used by decrypt_session/encrypt_session after rewriting a session's
    files under a new name (with or without the trailing ".enc") -- updates
    the index to match, so `korec ls`'s ENC column and every read path
    (which decides whether to even try resolving a key from `encrypted`,
    not from sniffing the filename) stay in sync with what's really on
    disk."""
    conn = _connect()
    conn.execute(
        "UPDATE sessions SET encrypted=?, cast_path=?, txt_path=? WHERE id=?",
        (int(encrypted), cast_path, txt_path, session_id),
    )
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
        f"""SELECT id, start_time, duration_sec, remote_host, tty, cast_size, exit_code, end_time, pid, encrypted
            FROM sessions {clause} ORDER BY start_time DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()

    fmt = "{:<5} {:<26} {:>7} {:<20} {:<12} {:>7} {:>8} {:>3}"
    print(fmt.format("ID", "START", "DUR(s)", "HOST", "TTY", "SIZE", "STATUS", "ENC"))
    for id_, start, dur, rhost, tty, size, rc, end_time, pid, encrypted in rows:
        dur_display = dur if dur is not None else "-"
        print(fmt.format(id_, start or "", dur_display, rhost or "", tty or "",
                          _human_size(size), _status(end_time, rc, pid), "*" if encrypted else ""))


def print_session(session_id: int) -> None:
    row = get_session(session_id)
    if row is None:
        sys.exit(f"korec: session {session_id} not found")
    for key in row.keys():
        print(f"{key}: {row[key]}")
    print(f"status: {_status(row['end_time'], row['exit_code'], row['pid'])}")


def _parse_transcript_line(line: str) -> tuple[float | None, str]:
    """Split a rendered transcript line back into (elapsed_seconds, content).
    Returns elapsed=None for sidecars rendered before per-line timestamps
    were tracked (plain content, no leading `<seconds>\\t`), so old
    recordings still grep/cat fine, just without a timestamp."""
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
    instead of the rendered .txt sidecar -- which only gets built once the
    session ends. Mirrors what the background `_render` step does, just
    synchronously and against a possibly mid-line stream.

    A running session has no `.cast.zst[.enc]` yet -- asciinema writes
    straight to the plain, uncompressed file at
    `recorder.raw_cast_path(cast_path)`, which only gets compressed (and,
    if configured, encrypted) into `cast_path` once `record()` finishes
    (see recorder.py). Read that instead when the compressed one isn't
    there yet -- note it's always plaintext, regardless of the session's
    `encrypted` flag, since asciinema itself writes it and has no idea
    korec might encrypt the final artifact.

    Returns None if nothing usable has been flushed to disk yet."""
    from .recorder import raw_cast_path, sanitize_cast_data
    from .render import render_raw_cast

    cast_path = row["cast_path"]
    p = Path(cast_path)
    try:
        if p.exists():
            if row["encrypted"]:
                raw = decrypt_file_with_retry(p, config.resolve_password(), f"session {row['id']}")
                if raw is None:
                    return None
            else:
                raw = decompress_file(p)
        else:
            raw_p = raw_cast_path(cast_path)
            if not raw_p.exists():
                return None
            raw = raw_p.read_bytes()
        data = sanitize_cast_data(raw)
        if data is None:
            return None
        return render_raw_cast(data.decode("utf-8", "replace"))
    except Exception as e:
        print(f"korec: couldn't render live transcript for {cast_path}: {e}", file=sys.stderr)
        return None


def _session_text(row: sqlite3.Row) -> str | None:
    """This session's rendered transcript (tab-timestamped, one line per
    output line) -- live-rendered from the cast file if it's still
    recording, otherwise read from the finished .txt sidecar. Prompts for
    (and retries) a password itself if the session is encrypted -- see
    compress.py's decrypt_file_with_retry. None if nothing's available yet
    either way, or decryption never succeeds."""
    if row["end_time"] is None:
        return _live_transcript(row)
    txt_path = row["txt_path"]
    if not txt_path or not Path(txt_path).exists():
        return None
    if not row["encrypted"]:
        try:
            return decompress_file(Path(txt_path)).decode("utf-8", "ignore")
        except Exception:
            return None
    raw = decrypt_file_with_retry(Path(txt_path), config.resolve_password(), f"session {row['id']}")
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
        source = row["cast_path"] if live else row["txt_path"]

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


def _strip_enc_suffix(p: Path) -> Path:
    return p.with_suffix("") if p.suffix == ".enc" else p


def _add_enc_suffix(p: Path) -> Path:
    return p if p.suffix == ".enc" else p.with_name(p.name + ".enc")


def _rewrite_session_files(
    session_id: int, row: sqlite3.Row, *, write_password: str | None, rename: Callable[[Path], Path],
) -> None:
    """Shared machinery for decrypt_session/encrypt_session: read each of a
    session's existing files -- decrypting, with an interactive retry-on-
    wrong-password (see compress.py's decrypt_file_with_retry), if
    `row["encrypted"]`; reading as plain otherwise. Note this is NOT the
    same thing as "a password happens to be resolvable" -- a session that
    genuinely has no password available yet still needs the encrypted
    read path (so decrypt_file_with_retry gets a chance to prompt), while
    a session that isn't encrypted at all must never be run through
    decryption just because some password happens to be lying around.

    Writes each file back out under `rename(original_path)`, encrypted
    with `write_password` if given, plain otherwise, then removes the old
    file if the name actually changed. Only touches files that exist -- a
    session's .txt.zst[.enc] sidecar may not be built yet."""
    cast_path = Path(row["cast_path"])
    txt_path = Path(row["txt_path"]) if row["txt_path"] else None
    new_cast_path = rename(cast_path)
    new_txt_path = rename(txt_path) if txt_path is not None else None
    read_password = config.resolve_password() if row["encrypted"] else None

    def read(path: Path) -> bytes:
        if not row["encrypted"]:
            return decompress_file(path)
        raw = decrypt_file_with_retry(path, read_password, f"session {session_id}")
        if raw is None:
            sys.exit(f"korec: giving up on session {session_id} -- couldn't decrypt {path}")
        return raw

    if cast_path.exists():
        raw = read(cast_path)
        compress_bytes_to_file(raw, new_cast_path, password=write_password)
        if new_cast_path != cast_path:
            cast_path.unlink()

    if txt_path is not None and txt_path.exists():
        raw = read(txt_path)
        compress_bytes_to_file(raw, new_txt_path, password=write_password)
        if new_txt_path != txt_path:
            txt_path.unlink()

    _set_session_crypto_state(
        session_id,
        encrypted=write_password is not None,
        cast_path=str(new_cast_path),
        txt_path=str(new_txt_path) if new_txt_path is not None else None,
    )


def decrypt_session(session_id: int) -> None:
    """Permanently decrypts an encrypted session's stored files in place:
    rewrites `*.cast.zst.enc`/`*.txt.zst.enc` as plain `*.cast.zst`/
    `*.txt.zst` (dropping the `.enc`), and clears the session's `encrypted`
    flag in the index -- no password needed to read it, ever again. There's
    no undo built into this specific direction; re-encrypt with
    `encrypt_session` if you want it back."""
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

    _rewrite_session_files(session_id, row, write_password=None, rename=_strip_enc_suffix)
    print(f"korec: session {session_id} decrypted in place -- no password needed for it anymore")


def encrypt_session(session_id: int) -> None:
    """The mirror of decrypt_session: encrypts a currently-plain session's
    stored files in place with the configured password, renaming them to
    `*.cast.zst.enc`/`*.txt.zst.enc` and setting the session's `encrypted`
    flag."""
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

    _rewrite_session_files(session_id, row, write_password=password, rename=_add_enc_suffix)
    print(f"korec: session {session_id} encrypted in place")
