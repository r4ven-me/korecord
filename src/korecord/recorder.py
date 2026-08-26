from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config, db
from .compress import compress_bytes_to_file, decompress_file, decrypt_file_with_retry

MIN_ASCIINEMA_MAJOR = 3


def _asciinema_major_version(path: str) -> int | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    # e.g. "asciinema 3.2.1" -> 3
    parts = out.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].split(".")[0])
    except ValueError:
        return None


# Fallback locations checked when `asciinema` isn't on $PATH -- exactly
# where the two install methods this project documents (README
# "Requirements") actually land it. korec is itself commonly launched this
# same way (a terminal profile's *custom command*, see README "Terminal-
# emulator integration"), which typically execs the binary directly with
# whatever bare $PATH the desktop/window-manager session set up -- no
# ~/.local/bin, no ~/.cargo/bin, since those are usually appended by
# .bashrc/.zshrc, which a directly-exec'd custom command never sources.
_FALLBACK_ASCIINEMA_PATHS = (
    Path.home() / ".local" / "bin" / "asciinema",
    Path.home() / ".cargo" / "bin" / "asciinema",
)


def find_asciinema() -> str:
    """korec shells out to the `asciinema` CLI rather than bundling it as a
    pip dependency. The modern CLI (3.x) is a Rust rewrite distributed as a
    standalone binary -- it isn't published to PyPI at all, so there's no
    version pip could ever install; the last PyPI release (2.4.0, Oct 2023)
    is the old Python line, permanently frozen there. Install the current
    release yourself from https://github.com/asciinema/asciinema/releases
    and make sure it's on $PATH.

    3.x specifically is required: `record` relies on `--return` (to get the
    recorded command's real exit code back, instead of the printf-into-a-
    tempfile side channel the 2.x integration needed) and `-f asciicast-v2`
    (korec's renderer still reads/writes the v2 file shape) -- neither flag
    exists on the old 2.x line."""
    found = shutil.which("asciinema")
    if not found:
        for candidate in _FALLBACK_ASCIINEMA_PATHS:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                found = str(candidate)
                break
    if not found:
        sys.exit(
            "korec: 'asciinema' not found on $PATH -- install the current release from "
            "https://github.com/asciinema/asciinema/releases (korec needs 3.x; the old "
            "`pip install asciinema` line stops at 2.4.0 and won't work)"
        )
    major = _asciinema_major_version(found)
    if major is None:
        print(f"korec: couldn't determine {found}'s version -- proceeding anyway", file=sys.stderr)
    elif major < MIN_ASCIINEMA_MAJOR:
        sys.exit(
            f"korec: found asciinema at {found}, but it's too old for korec (needs "
            f"{MIN_ASCIINEMA_MAJOR}.x+). Install the current release from "
            "https://github.com/asciinema/asciinema/releases"
        )
    return found


def sanitize(s: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in s)
    return cleaned or "unknown"


def default_label(command: list[str]) -> str:
    if not command:
        return "unknown"
    prog = Path(command[0]).name
    if prog in ("ssh", "mosh") and len(command) > 1:
        target = next((a for a in command[1:] if not a.startswith("-")), None)
        if target:
            target = target.split("@")[-1].split(":")[0]
            return sanitize(target)
    return sanitize(prog)


def tty_name() -> str:
    try:
        out = subprocess.run(["tty"], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        out = ""
    if out.startswith("/dev/"):
        return out.removeprefix("/dev/").replace("/", "_")
    return "notty"


def raw_cast_path(cast_path: Path | str) -> Path:
    """The plain, uncompressed file asciinema writes to directly while a
    session is recording -- `cast_path` with its ".zst" (and, for an
    encrypted session, its trailing ".enc") suffix stripped. asciinema 3.x
    requires a real file path (no more streaming to stdout), so this is
    what actually gets written live; it only exists for the duration of
    the recording, and is deleted once `record()` compresses (and, if
    enabled, encrypts) it into `cast_path` at the end. A session that's
    still running (or one whose compression step never got to run because
    `korec record` itself died first) is found here instead -- always
    plain, never encrypted, regardless of the session's `encrypted` flag,
    since asciinema itself writes it directly and has no idea korec might
    encrypt the finished artifact."""
    p = Path(cast_path)
    if p.suffix == ".enc":
        p = p.with_suffix("")
    return p.with_suffix("")


def _descendant_pids(pid: int) -> list[int]:
    """Every process in /proc descended from `pid` (children, grandchildren,
    ...), found by scanning /proc/*/stat for each process's parent pid.
    Linux-specific -- so is the rest of korecord's process-liveness
    handling (see db._pid_alive's os.kill(pid, 0) probe)."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue  # the process exited between listdir() and read()
        # Format: "pid (comm) state ppid ...". comm can itself contain
        # spaces/parens, so split from the *last* ")" rather than naively
        # splitting on whitespace from the start.
        rparen = stat.rfind(")")
        fields = stat[rparen + 2:].split()
        ppid = int(fields[1])  # fields[0] is state, fields[1] is ppid
        children.setdefault(ppid, []).append(int(entry))

    result = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            result.append(child)
            stack.append(child)
    return result


def _kill_process_tree(pid: int) -> None:
    """SIGKILL `pid` and every descendant of it.

    Used when korec itself is being torn down by SIGTERM/SIGHUP (terminal
    closed, window manager crashed, ...) to make sure the asciinema
    process it's recording through -- and anything *that* spawned, e.g.
    the ssh it's running -- don't survive as orphans holding a connection
    open forever. asciinema deliberately ignores SIGTERM/SIGHUP itself
    (reasonable, so a stray signal doesn't lose an in-progress recording),
    which is exactly what leaves it orphaned if korec dies without
    explicitly killing it first -- SIGKILL is the one signal nothing can
    catch or ignore.

    Deliberately doesn't put the child in its own process group/session to
    make this easier (e.g. via os.killpg) -- that would detach it from the
    controlling terminal, and with it SIGWINCH-driven resize handling
    (asciinema needs to know when the outer terminal resizes to record
    accurate "r" events and keep the inner pty it manages in sync)."""
    pids = _descendant_pids(pid) + [pid]
    # Deepest descendants first, so a parent dying doesn't get a child
    # reparented to init before its turn -- purely cosmetic, since SIGKILL
    # is unconditional either way, but avoids a brief orphan window.
    for p in reversed(pids):
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


def record(command: list[str], label: str | None = None) -> int:
    if not command:
        sys.exit("korec record: no command given, e.g. `korec record -- ssh myhost`")

    session_label = sanitize(label) if label else default_label(command)
    tty = tty_name()
    start = datetime.now().astimezone()
    root = config.data_dir() / session_label / f"{start:%Y}" / f"{start:%m}"
    root.mkdir(parents=True, exist_ok=True)

    # Resolved once, up front, in this (foreground, interactive-capable)
    # process -- not in the detached `_render` background process spawned
    # below, which has no stdin to prompt on. Failing loudly here beats
    # silently recording in plaintext when the user explicitly turned
    # encryption on.
    encrypted = config.encryption_enabled()
    password = None
    if encrypted:
        password = config.resolve_password()
        if password is None:
            sys.exit(
                "korec: encryption is enabled (`korec config encryption show`) but no "
                "password is available -- set $KORECORD_PASSWORD, store one via `korec "
                "config encryption enable`, or run this interactively so korec can prompt"
            )

    # The pid suffix guarantees uniqueness even when two sessions start in
    # the same second on the same tty name -- which real, distinct ptys
    # never share, but a "notty" fallback (no controlling terminal, e.g.
    # when invoked from a script/cron) could, and a filename collision
    # there would silently overwrite one session's recording with another's.
    base = f"{start:%Y-%m-%d_%H%M%S}_{tty}_{os.getpid()}"
    # An extra ".enc" makes encrypted recordings identifiable straight from
    # a file listing (backup tooling, `ls`, ...) without needing to consult
    # the index -- and is exactly what tells `raw_cast_path()` to strip it.
    enc_suffix = ".enc" if encrypted else ""
    cast_path = root / f"{base}.cast.zst{enc_suffix}"
    txt_path = root / f"{base}.txt.zst{enc_suffix}"
    raw_path = raw_cast_path(cast_path)

    # Inserted *before* recording starts (end_time/exit_code left NULL), so
    # a session already shows up in `korec ls` while it's still running --
    # and if this process gets killed outright, the row survives as
    # evidence the session happened, with its status inferred from whether
    # `pid` is still alive.
    local_host = subprocess.run(["hostname"], capture_output=True, text=True, timeout=2).stdout.strip()
    session_id = db.insert_pending_session(
        start=start.isoformat(timespec="seconds"),
        local_host=local_host,
        remote_host=session_label,
        tty=tty,
        cast_path=str(cast_path),
        txt_path=str(txt_path),
        pid=os.getpid(),
        encrypted=encrypted,
    )

    asciinema = find_asciinema()
    cmd_str = " ".join(shlex.quote(a) for a in command)

    # `-f asciicast-v2` keeps asciinema writing the file shape korec's
    # renderer already understands -- v3's native format (terminal size
    # nested under `term.cols/rows`, event timestamps as deltas rather than
    # absolute offsets) would need matching changes throughout
    # render.py/db.py for no benefit korec actually uses. `--return` makes
    # asciinema itself exit with the recorded command's real status, so
    # there's no more need to smuggle $? out through a side-channel tempfile.
    proc = subprocess.Popen([
        asciinema, "record", "-f", "asciicast-v2", "--overwrite", "-q", "-i", "2",
        "-c", cmd_str, "--return", str(raw_path),
    ])

    # If korec itself gets torn down by SIGTERM/SIGHUP -- a closed
    # terminal tab, a crashed window manager, anything that isn't a clean
    # exit -- make sure asciinema (and whatever it's running, e.g. ssh)
    # dies with it instead of surviving as an orphan; see
    # _kill_process_tree. The handler doesn't itself terminate korec: it
    # just kills the recording, then returns, letting the interrupted
    # proc.wait() below transparently retry (per PEP 475) and pick up the
    # now-dead child -- so the rest of this function still runs and
    # finalizes the session normally, just with a duration that stops
    # where the signal arrived instead of hanging forever.
    def _kill_recording_on_teardown(signum: int, frame) -> None:
        _kill_process_tree(proc.pid)

    previous_term = signal.signal(signal.SIGTERM, _kill_recording_on_teardown)
    previous_hup = signal.signal(signal.SIGHUP, _kill_recording_on_teardown)
    try:
        exit_code = proc.wait()
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGHUP, previous_hup)

    end = datetime.now().astimezone()
    duration = int((end - start).total_seconds())

    # asciinema flushes every event to `raw_path` as it happens (confirmed:
    # output shows up within the same second it's produced), so by the time
    # `record` above returns there's nothing left to stream -- just turn
    # the finished raw file into the zstd-compressed artifact everything
    # else (grep, cat, play) expects, then drop the plain copy.
    if raw_path.exists():
        try:
            compress_bytes_to_file(raw_path.read_bytes(), cast_path, password=password)
            cast_size = cast_path.stat().st_size
        finally:
            raw_path.unlink(missing_ok=True)
    else:
        cast_size = 0

    # Detached (own session, like `setsid`) so it survives the terminal
    # closing right after the recorded command exits. `--encrypted` tells
    # it definitively whether to encrypt the transcript -- NOT whether a
    # password happens to be resolvable, since $KORECORD_PASSWORD could be
    # sitting in the ambient environment for unrelated reasons and using
    # that alone would risk wrongly encrypting a plain session. The
    # password itself, when needed, travels via that same env var (reusing
    # the one `resolve_password()` already checks first everywhere else),
    # not argv, so it doesn't show up in `ps`/process listings.
    render_cmd = [sys.executable, "-m", "korecord", "_render", str(cast_path), str(txt_path)]
    render_env = None
    if encrypted:
        render_cmd.append("--encrypted")
        render_env = {**os.environ, "KORECORD_PASSWORD": password}
    subprocess.Popen(
        render_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=render_env,
    )

    db.finalize_session(
        session_id,
        end=end.isoformat(timespec="seconds"),
        duration=duration,
        cast_size=cast_size,
        exit_code=exit_code,
    )

    return exit_code


def sanitize_cast_data(data: bytes) -> bytes | None:
    """Trim at the first line that isn't valid JSON, instead of handing
    asciinema a stream it'll crash on. Encountered when playing a session
    that's still recording (its file only has whatever's been flushed so
    far, which can end mid-line) or one truncated by a hard crash -- in
    both cases everything up to that point is still good and worth
    playing; only the incomplete tail needs dropping.

    Returns None if there isn't even a usable header yet (e.g. a session
    that just started and hasn't flushed anything)."""
    lines = data.split(b"\n")
    if not lines or not lines[0].strip():
        return None
    try:
        json.loads(lines[0])
    except (ValueError, json.JSONDecodeError):
        return None

    good = lines[:1]
    truncated = False
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            json.loads(line)
        except (ValueError, json.JSONDecodeError):
            truncated = True
            break
        good.append(line)
    if truncated:
        print("korec: the tail of this recording isn't complete yet -- playing back what's available so far", file=sys.stderr)
    return b"\n".join(good) + b"\n"


def play(session_id: int) -> None:
    row = db.get_session(session_id)
    if row is None:
        sys.exit(f"korec: session {session_id} not found")
    cast_path = Path(row["cast_path"])
    if cast_path.exists():
        if row["encrypted"]:
            raw = decrypt_file_with_retry(cast_path, config.resolve_password(), f"session {session_id}")
            if raw is None:
                sys.exit(f"korec: couldn't decrypt session {session_id}")
        else:
            raw = decompress_file(cast_path)
    else:
        # The plain file asciinema writes to live, during an active
        # recording -- always unencrypted regardless of the session's
        # `encrypted` flag, since asciinema itself writes it directly and
        # has no idea korec encrypts the finished artifact.
        raw_path = raw_cast_path(cast_path)
        if not raw_path.exists():
            sys.exit(f"korec: {cast_path} is missing")
        raw = raw_path.read_bytes()
    if row["end_time"] is None:
        print(f"korec: session {session_id} is still recording -- playing back what's been written so far", file=sys.stderr)
    data = sanitize_cast_data(raw)
    if data is None:
        sys.exit(f"korec: nothing recorded yet for session {session_id}")
    asciinema = find_asciinema()
    subprocess.run([asciinema, "play", "-"], input=data)
