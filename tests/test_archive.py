from __future__ import annotations

from korecord import archive


def test_create_and_read_member(tmp_path):
    path = tmp_path / "s.rec"
    archive.create(path, "cast", b"hello cast")
    assert archive.read_member(path, "cast") == b"hello cast"


def test_read_missing_member_returns_none(tmp_path):
    path = tmp_path / "s.rec"
    archive.create(path, "cast", b"hello cast")
    assert archive.read_member(path, "txt") is None


def test_append_adds_a_member_without_disturbing_the_first(tmp_path):
    path = tmp_path / "s.rec"
    archive.create(path, "cast", b"hello cast")
    archive.append(path, "txt", b"hello txt")
    assert archive.read_member(path, "cast") == b"hello cast"
    assert archive.read_member(path, "txt") == b"hello txt"


def test_has_member(tmp_path):
    path = tmp_path / "s.rec"
    archive.create(path, "cast", b"hello cast")
    assert archive.has_member(path, "cast") is True
    assert archive.has_member(path, "txt") is False
    archive.append(path, "txt", b"hello txt")
    assert archive.has_member(path, "txt") is True


def test_create_overwrites_an_existing_archive(tmp_path):
    path = tmp_path / "s.rec"
    archive.create(path, "cast", b"first")
    archive.append(path, "txt", b"first-txt")
    archive.create(path, "cast", b"second")
    assert archive.read_member(path, "cast") == b"second"
    assert archive.read_member(path, "txt") is None  # overwritten from scratch


def test_create_makes_parent_directories(tmp_path):
    path = tmp_path / "nested" / "deeper" / "s.rec"
    archive.create(path, "cast", b"x")
    assert path.exists()
    assert archive.read_member(path, "cast") == b"x"


def test_empty_member_roundtrips(tmp_path):
    path = tmp_path / "s.rec"
    archive.create(path, "cast", b"")
    assert archive.read_member(path, "cast") == b""


def test_binary_member_roundtrips(tmp_path):
    path = tmp_path / "s.rec"
    data = bytes(range(256)) * 10
    archive.create(path, "cast", data)
    assert archive.read_member(path, "cast") == data
