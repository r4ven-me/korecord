from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own throwaway data dir/index db via
    $KORECORD_DATA_DIR -- never touches the real ~/.local/share/korecord.
    Also isolates the config directory (via $XDG_CONFIG_HOME) -- config.json
    is where an encryption password can end up stored, so tests must never
    read or write the real ~/.config/korecord/config.json -- and clears
    $KORECORD_PASSWORD so a developer's own env doesn't leak into password
    -resolution tests."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("KORECORD_DATA_DIR", str(data_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("KORECORD_PASSWORD", raising=False)
    return data_dir
