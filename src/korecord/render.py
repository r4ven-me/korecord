"""Build a searchable plaintext transcript from a compressed asciicast by
replaying it through a real terminal emulator (pyte), rather than just
stripping ANSI color codes with a regex. A naive regex strip leaves
\\r-redraws and line-clear sequences (zsh autosuggestions, powerlevel10k,
progress bars, ...) as separate concatenated lines instead of collapsing
them to what was actually on screen -- pyte renders the real final state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyte
from pyte.screens import Margins
from wcwidth import wcwidth

from . import archive
from .compress import pack_bytes, unpack_bytes

HISTORY_LINES = 5_000_000


def _render_line(line, columns: int) -> str:
    def gen():
        is_wide = False
        for x in range(columns):
            if is_wide:
                is_wide = False
                continue
            ch = line[x].data
            is_wide = bool(ch) and wcwidth(ch[0]) == 2
            yield ch
    return "".join(gen()).rstrip()


class _TimingScreen(pyte.HistoryScreen):
    """A HistoryScreen that additionally tracks, per row, the timestamp of
    the event that last actually wrote text into it.

    This deliberately does NOT use pyte's own `screen.dirty` set for that:
    `Screen.index()` marks the *entire* screen dirty on every scroll (every
    row's on-screen *position* shifted, which is what a redraw-oriented
    consumer cares about), even though only one row's *content* actually
    changed. Treating "dirty" as "written just now" smears a single
    scroll's timestamp across the whole screen the moment a session
    produces more than one screenful of output -- everything after the
    first scroll would collapse to whichever event happened to be running
    when `history.top` last grew. Hooking `draw()` (the actual "characters
    were written at the cursor" primitive) and `index()` (the actual
    "a row moved into history" primitive) tracks the real thing instead.

    Callers must set `current_t` before each `feed()` call they want
    attributed correctly."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_t = 0.0
        self.row_times: list[float] = [0.0] * self.lines
        self.history_times: list[float] = []

    def draw(self, text: str) -> None:
        before_y = self.cursor.y
        super().draw(text)
        after_y = self.cursor.y
        lo, hi = (before_y, after_y) if before_y <= after_y else (after_y, before_y)
        for y in range(lo, hi + 1):
            if 0 <= y < len(self.row_times):
                self.row_times[y] = self.current_t

    def index(self) -> None:
        top, bottom = self.margins or Margins(0, self.lines - 1)
        scrolling = self.cursor.y == bottom
        super().index()
        if scrolling and self.row_times:
            self.history_times.append(self.row_times.pop(0))
            self.row_times.append(self.current_t)

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        super().resize(lines, columns)
        new_lines = self.lines
        if new_lines > len(self.row_times):
            self.row_times += [self.current_t] * (new_lines - len(self.row_times))
        else:
            self.row_times = self.row_times[:new_lines]


def render_raw_cast(raw: str) -> str:
    """Replay a decompressed asciicast (JSONL) through pyte and return the
    resulting screen text, one rendered line per output line, each prefixed
    with `<elapsed-seconds>\\t` -- the recording-relative timestamp of the
    event that actually wrote that line's text (see `_TimingScreen`). Tab-
    separated rather than baked into a human format so matching against the
    actual content (`korec grep`) doesn't accidentally match digits in a
    timestamp -- callers format it for display.

    Split out from `render_cast_to_text` so a still-recording session's
    partial cast data can be rendered live for `korec grep`, not just a
    finished session's.

    The timestamp is only approximate to the granularity of the recording
    itself: a single event can carry a burst of output spanning several
    lines under one timestamp. Good enough to jump near the right spot in
    `korec cat | less`, not frame-accurate."""
    lines = raw.splitlines()
    if not lines:
        return ""
    try:
        header = json.loads(lines[0])
    except (ValueError, json.JSONDecodeError):
        return ""
    cols = header.get("width", 80)
    rows = header.get("height", 24)

    screen = _TimingScreen(cols, rows, history=HISTORY_LINES)
    stream = pyte.Stream(screen)

    last_t = 0.0

    for raw_line in lines[1:]:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            t, etype, data = json.loads(raw_line)
        except (ValueError, json.JSONDecodeError):
            continue
        last_t = t
        screen.current_t = t
        if etype == "o":
            try:
                stream.feed(data)
            except Exception:
                # pyte can choke on a handful of obscure/malformed escape
                # sequences (e.g. a DEC-private-prefixed SGR like `\x1b[?4m`
                # -- select_graphic_rendition() doesn't take the `private`
                # kwarg pyte's CSI parser passes for any `?`-prefixed
                # sequence, not just set_mode/reset_mode). pyte already
                # resets its own parser state before re-raising (see
                # Stream._send_to_parser), so it's safe to just skip this
                # chunk and keep feeding -- one malformed sequence shouldn't
                # kill the whole transcript.
                continue
        elif etype == "r":
            try:
                w, h = data.split("x")
                screen.resize(lines=int(h), columns=int(w))
            except ValueError:
                pass

    top_lines = [_render_line(l, screen.columns) for l in screen.history.top]
    history_times = screen.history_times
    # A resize can reflow `history.top` in ways that don't line up 1:1 with
    # `history_times` -- pad/truncate rather than mis-index into it.
    if len(history_times) < len(top_lines):
        history_times = history_times + [last_t] * (len(top_lines) - len(history_times))
    elif len(history_times) > len(top_lines):
        history_times = history_times[-len(top_lines):]

    row_times = screen.row_times
    out_lines = list(zip(history_times, top_lines))
    out_lines += [
        (row_times[y] if y < len(row_times) else last_t, l.rstrip())
        for y, l in enumerate(screen.display)
    ]
    while out_lines and not out_lines[-1][1]:
        out_lines.pop()

    return "".join(f"{t:.3f}\t{text}\n" for t, text in out_lines)


def render_cast_member_to_txt(path: Path, *, compressed: bool, password: str | None = None) -> None:
    """Reads the "cast" member out of a session's `.rec` archive at `path`,
    builds its searchable transcript, and appends it back as a "txt"
    member -- packed the same way (`compressed`/`password`) as the cast
    member already is, so the two stay consistent. Each member gets its
    own independently-generated salt (see crypto.py) even though they
    share the same password -- no retry-on-wrong-password here, unlike
    compress.py's unpack_bytes_with_retry: this runs as a detached
    background process with no stdin to prompt on, so there'd be nothing
    to retry with anyway."""
    try:
        blob = archive.read_member(path, "cast")
        raw = unpack_bytes(blob, compressed=compressed, password=password).decode("utf-8", "replace")
        text = render_raw_cast(raw)
    except Exception:
        text = ""
    archive.append(path, "txt", pack_bytes(text.encode("utf-8"), compress=compressed, password=password))
