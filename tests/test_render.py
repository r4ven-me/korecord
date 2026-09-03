from __future__ import annotations

import json

from korecord import archive
from korecord.compress import pack_bytes, unpack_bytes
from korecord.render import render_cast_member_to_txt, render_raw_cast


def make_cast(events, width=80, height=24):
    header = json.dumps({"version": 2, "width": width, "height": height})
    lines = [header] + [json.dumps(list(e)) for e in events]
    return "\n".join(lines) + "\n"


def parse_out(out: str) -> list[tuple[float, str]]:
    result = []
    for line in out.splitlines():
        head, _, content = line.partition("\t")
        result.append((float(head), content))
    return result


def test_empty_input_returns_empty():
    assert render_raw_cast("") == ""


def test_malformed_header_returns_empty():
    assert render_raw_cast("not json at all\n") == ""


def test_basic_lines_rendered_in_order():
    raw = make_cast([(0.0, "o", "hello\r\n"), (0.1, "o", "world\r\n")])
    contents = [c for _, c in parse_out(render_raw_cast(raw))]
    assert contents == ["hello", "world"]


def test_ansi_sgr_codes_stripped_from_content():
    raw = make_cast([(0.0, "o", "\x1b[31mred text\x1b[0m\r\n")])
    contents = [c for _, c in parse_out(render_raw_cast(raw))]
    assert contents == ["red text"]
    assert "\x1b" not in contents[0]


def test_malformed_json_event_line_skipped(monkeypatch):
    raw = make_cast([(0.0, "o", "before\r\n")])
    raw += "not valid json\n"
    raw += json.dumps([0.1, "o", "after\r\n"])
    contents = [c for _, c in parse_out(render_raw_cast(raw))]
    assert contents == ["before", "after"]


def test_resize_event_does_not_crash():
    raw = make_cast([
        (0.0, "o", "one\r\n"),
        (1.0, "r", "40x10"),
        (2.0, "o", "two\r\n"),
    ])
    contents = [c for _, c in parse_out(render_raw_cast(raw))]
    assert contents == ["one", "two"]


def test_malformed_resize_dimensions_ignored():
    raw = make_cast([
        (0.0, "o", "one\r\n"),
        (1.0, "r", "not-a-size"),
        (2.0, "o", "two\r\n"),
    ])
    contents = [c for _, c in parse_out(render_raw_cast(raw))]
    assert contents == ["one", "two"]


# --- regression: pyte crashes on a DEC-private-prefixed SGR sequence ------
# (e.g. `\x1b[?4m`) because Screen.select_graphic_rendition() doesn't take
# the `private` kwarg pyte's CSI parser passes for any `?`-prefixed CSI,
# not just set_mode/reset_mode. Found live in a real recorded ssh session
# -- it used to kill the whole `korec grep`/`_render` run.
def test_malformed_private_csi_sgr_sequence_is_skipped_not_fatal():
    raw = make_cast([
        (0.0, "o", "before\r\n"),
        (0.1, "o", "\x1b[?4m"),
        (0.2, "o", "after\r\n"),
    ])
    contents = [c for _, c in parse_out(render_raw_cast(raw))]
    assert contents == ["before", "after"]


def test_render_cast_member_to_txt_survives_corrupt_cast_member(tmp_path):
    path = tmp_path / "bad.rec"
    archive.create(path, "cast", b"this is not a valid zstd stream")
    render_cast_member_to_txt(path, compressed=True)  # must not raise
    assert unpack_bytes(archive.read_member(path, "txt")) == b""


def test_render_cast_member_to_txt_roundtrip(tmp_path):
    raw = make_cast([(0.0, "o", "hi there\r\n")])
    path = tmp_path / "in.rec"
    archive.create(path, "cast", pack_bytes(raw.encode()))
    render_cast_member_to_txt(path, compressed=True)
    text = unpack_bytes(archive.read_member(path, "txt")).decode()
    assert "hi there" in text
    assert "\t" in text  # tab-separated timestamp prefix made it into the sidecar


def test_render_cast_member_to_txt_uncompressed(tmp_path):
    raw = make_cast([(0.0, "o", "hi there\r\n")])
    path = tmp_path / "in.rec"
    archive.create(path, "cast", pack_bytes(raw.encode(), compress=False))
    render_cast_member_to_txt(path, compressed=False)
    text = unpack_bytes(archive.read_member(path, "txt"), compressed=False).decode()
    assert "hi there" in text


def test_render_cast_member_to_txt_encrypted(tmp_path):
    raw = make_cast([(0.0, "o", "secret line\r\n")])
    path = tmp_path / "in.rec"
    archive.create(path, "cast", pack_bytes(raw.encode(), password="hunter2"))
    render_cast_member_to_txt(path, compressed=True, password="hunter2")
    text = unpack_bytes(archive.read_member(path, "txt"), password="hunter2").decode()
    assert "secret line" in text


# --- regression: every visible line used to get the *last* event's
# timestamp instead of its own, because only history-scrolled lines were
# timestamped -- lines still on-screen at the end all collapsed to one
# timestamp. Caught by hand: three ticks a second apart all showed the same
# time.
def test_per_line_timestamps_differ_on_still_visible_screen():
    events = [(float(i), "o", f"line{i}\r\n") for i in range(1, 4)]
    raw = make_cast(events, height=24)  # well within the screen -- nothing scrolls
    parsed = parse_out(render_raw_cast(raw))
    assert [c for _, c in parsed] == ["line1", "line2", "line3"]
    assert [t for t, _ in parsed] == [1.0, 2.0, 3.0]


def test_per_line_timestamps_preserved_through_history_scroll():
    # A 2-row screen forced to scroll: lines 1..3 get pushed into history,
    # lines 4..5 remain visible. Every line should keep its own timestamp,
    # not collapse to the scrolling event's or the final event's time.
    events = [(float(i), "o", f"line{i}\r\n") for i in range(1, 6)]
    raw = make_cast(events, width=20, height=2)
    parsed = parse_out(render_raw_cast(raw))
    assert [c for _, c in parsed] == ["line1", "line2", "line3", "line4", "line5"]
    assert [t for t, _ in parsed] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_per_line_timestamps_correct_across_many_scrolls_at_realistic_scale():
    """Regression test for a real bug caught against an actual recording,
    not a synthetic one: pyte's `Screen.index()` marks the *entire* screen
    `dirty` on every scroll (every row's on-screen position shifted), not
    just the one row whose content actually changed. The first
    implementation used `screen.dirty` as "written just now", which was
    fine below one screenful -- but the moment a real ~30-line session
    scrolled past its 24-row screen, every event after the first scroll
    smeared its timestamp across all 24 rows, and every event after that
    kept re-smearing the same range. A small 2-row/5-line toy test didn't
    exercise this (accidentally passed either way); this uses a realistic
    24-row screen with enough lines to scroll repeatedly, and checks every
    single timestamp, not just the first handful."""
    n = 40
    events = [(float(i), "o", f"line{i}\r\n") for i in range(n)]
    raw = make_cast(events, height=24)
    parsed = parse_out(render_raw_cast(raw))
    assert [c for _, c in parsed] == [f"line{i}" for i in range(n)]
    assert [t for t, _ in parsed] == [float(i) for i in range(n)]


def test_timestamps_are_tab_separated_and_content_matchable():
    """The `<seconds>\\t<content>` format must let callers grep the content
    half without a numeric pattern accidentally matching the timestamp."""
    raw = make_cast([(12345.0, "o", "just some text\r\n")])
    out = render_raw_cast(raw)
    line = out.splitlines()[0]
    ts, _, content = line.partition("\t")
    assert ts == "12345.000"
    assert content == "just some text"
