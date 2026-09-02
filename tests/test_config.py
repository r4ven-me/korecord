from __future__ import annotations

import stat

from korecord import config


# --- data dir / config file plumbing (pre-existing behavior, previously
# untested directly) ----------------------------------------------------

def test_data_dir_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-data"
    monkeypatch.setenv("KORECORD_DATA_DIR", str(override))
    assert config.data_dir() == override


def test_set_and_unset_data_dir(tmp_path, monkeypatch):
    # $KORECORD_DATA_DIR (set by the autouse isolation fixture) takes
    # priority over the config file -- unset it here to actually exercise
    # the config-file-backed path this test is about.
    monkeypatch.delenv("KORECORD_DATA_DIR", raising=False)
    custom = tmp_path / "elsewhere"
    config.set_data_dir(str(custom))
    assert config.data_dir() == custom
    config.set_data_dir(None)
    assert config.data_dir() != custom


def test_save_config_restricts_file_permissions(tmp_path):
    config.set_data_dir(str(tmp_path / "x"))
    mode = stat.S_IMODE(config.config_file().stat().st_mode)
    assert mode == 0o600


def test_db_path_is_under_data_dir():
    assert config.db_path() == config.data_dir() / "index.db"


# --- encryption settings -------------------------------------------------
#
# No salt lives here anymore -- each encrypted file carries its own,
# embedded directly in it (see crypto.py). That's a deliberate fix for a
# real incident: keeping one shared salt in the config file meant losing
# that file (separately from the data) orphaned every encrypted session
# even with the exact right password re-entered, because a fresh salt got
# generated in its place and a salt has to match precisely what encrypted
# the data. config.py now only ever needs to hand back *a* password to try.

def test_encryption_disabled_by_default():
    assert config.encryption_enabled() is False
    assert config.encryption_stored_password() is None


def test_set_encryption_enabled_with_stored_password():
    config.set_encryption(enabled=True, password="hunter2", store_password=True)
    assert config.encryption_enabled() is True
    assert config.encryption_stored_password() == "hunter2"


def test_set_encryption_without_storing_password_leaves_it_unset():
    config.set_encryption(enabled=True, password="hunter2", store_password=False)
    assert config.encryption_enabled() is True
    assert config.encryption_stored_password() is None


def test_disabling_encryption_does_not_wipe_stored_password():
    """Regression test: an earlier version of set_encryption() defaulted
    store_password to False, so `korec config encryption disable` (which
    only intends to flip `enabled` off) accidentally erased the stored
    password too -- making previously-encrypted sessions unreadable
    without re-entering it. Toggling `enabled` alone (store_password left
    at its default, None) must leave the stored password untouched."""
    config.set_encryption(enabled=True, password="hunter2", store_password=True)
    config.set_encryption(enabled=False)
    assert config.encryption_enabled() is False
    assert config.encryption_stored_password() == "hunter2"


def test_set_encryption_store_password_false_clears_it_explicitly():
    config.set_encryption(enabled=True, password="hunter2", store_password=True)
    config.set_encryption(enabled=True, password="hunter2", store_password=False)
    assert config.encryption_stored_password() is None


# --- optional compression --------------------------------------------------

def test_compression_enabled_by_default():
    assert config.compression_enabled() is True


def test_set_compression_disabled():
    config.set_compression(False)
    assert config.compression_enabled() is False


def test_set_compression_re_enabled():
    config.set_compression(False)
    config.set_compression(True)
    assert config.compression_enabled() is True


def test_compression_setting_independent_of_encryption():
    config.set_compression(False)
    config.set_encryption(enabled=True, password="hunter2", store_password=True)
    assert config.compression_enabled() is False
    assert config.encryption_enabled() is True


# --- password resolution ---------------------------------------------------

def test_resolve_password_from_env_var(monkeypatch):
    monkeypatch.setenv("KORECORD_PASSWORD", "from-env")
    assert config.resolve_password() == "from-env"


def test_resolve_password_from_config(monkeypatch):
    monkeypatch.delenv("KORECORD_PASSWORD", raising=False)
    config.set_encryption(enabled=True, password="from-config", store_password=True)
    assert config.resolve_password() == "from-config"


def test_resolve_password_env_var_takes_priority_over_config(monkeypatch):
    config.set_encryption(enabled=True, password="from-config", store_password=True)
    monkeypatch.setenv("KORECORD_PASSWORD", "from-env")
    assert config.resolve_password() == "from-env"


def test_resolve_password_returns_none_without_prompting_when_not_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert config.resolve_password() is None


def test_resolve_password_prompts_when_tty_and_nothing_else_available(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "typed-in")
    assert config.resolve_password() == "typed-in"


def test_resolve_password_prompt_if_missing_false_never_prompts(monkeypatch):
    called = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": called.append(prompt) or "typed-in")
    assert config.resolve_password(prompt_if_missing=False) is None
    assert called == []
