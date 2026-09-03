from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "korecord"


def config_file() -> Path:
    return config_dir() / "config.json"


def _load_config() -> dict:
    path = config_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_config(cfg: dict) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_file()
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    # The config file can hold an encryption password in plaintext (see
    # set_encryption) -- restrict it to the owner regardless of whether
    # encryption is actually in use, since there's no downside to it.
    path.chmod(0o600)


def set_data_dir(path: str | None) -> None:
    """Persist a custom data directory (None clears it, reverting to the
    default). Written to config_file() so it applies even when korec is
    launched without a shell around it (e.g. as a terminal profile's
    "custom command"), where shell-rc-file env vars never get sourced."""
    cfg = _load_config()
    if path is None:
        cfg.pop("data_dir", None)
    else:
        cfg["data_dir"] = str(Path(path).expanduser())
    _save_config(cfg)


def data_dir() -> Path:
    """Root directory for recordings and the index DB. Resolution order:

    1. $KORECORD_DATA_DIR env var (handy for scripting/tests)
    2. "data_dir" in config_file(), set via `korec config set-data-dir`
    3. $XDG_DATA_HOME/korecord, falling back to ~/.local/share/korecord
    """
    override = os.environ.get("KORECORD_DATA_DIR")
    if override:
        return Path(override).expanduser()

    configured = _load_config().get("data_dir")
    if configured:
        return Path(configured).expanduser()

    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "korecord"


def db_path() -> Path:
    return data_dir() / "index.db"


# --- optional encryption -----------------------------------------------
#
# Threat model: protecting a *copy* of the data directory (backup, synced
# folder, old disk) that ends up separated from the config file -- not
# defending against a local attacker who already has filesystem access to
# both. That's the whole justification for letting the config file hold
# the password at all (see crypto.py for more).
#
# Note there's no salt stored here -- each file carries its own, embedded
# directly in it (see crypto.py), so the password alone is always enough.

def encryption_settings() -> dict:
    return _load_config().get("encryption", {})


def encryption_enabled() -> bool:
    """Whether *new* recordings should be encrypted. Existing encrypted
    sessions stay readable (given the password) regardless of this --
    each session's own `encrypted` flag in the index decides how it was
    written, not this live setting."""
    return bool(encryption_settings().get("enabled", False))


def encryption_stored_password() -> str | None:
    return encryption_settings().get("password")


def set_encryption(*, enabled: bool, password: str | None = None, store_password: bool | None = None) -> None:
    """Persist encryption settings.

    `store_password`: True stores `password` in the config file; False
    explicitly clears any previously-stored password; None (the default)
    leaves whatever's currently stored untouched -- e.g. `korec config
    encryption disable` only needs to flip `enabled` off, and must NOT
    wipe the stored password in the process, or existing encrypted
    sessions become unreadable until it's set up again."""
    cfg = _load_config()
    enc = cfg.get("encryption", {})
    enc["enabled"] = enabled
    if store_password is True and password is not None:
        enc["password"] = password
    elif store_password is False:
        enc.pop("password", None)
    cfg["encryption"] = enc
    _save_config(cfg)


# --- optional compression -----------------------------------------------
#
# On by default. Unlike encryption there's no secret to manage here, just
# a plain on/off switch -- disable it only for raw throughput (skips the
# zstd pass entirely), e.g. a very high-output session on a slow machine
# where compression itself becomes the bottleneck. A session's own
# `compressed` flag in the index records which way it was actually
# written, independent of whatever this is set to later.

def compression_enabled() -> bool:
    return bool(_load_config().get("compression", {}).get("enabled", True))


def set_compression(enabled: bool) -> None:
    cfg = _load_config()
    comp = cfg.get("compression", {})
    comp["enabled"] = enabled
    cfg["compression"] = comp
    _save_config(cfg)


def resolve_password(*, prompt_if_missing: bool = True) -> str | None:
    """Resolution order: $KORECORD_PASSWORD env var (scripting/CI), the
    config file (if `korec config encryption enable` stored it there), then
    an interactive prompt -- only if stdin is actually a tty, since a
    detached process (the background transcript renderer) can't prompt for
    anything and would otherwise hang forever.

    This is just *a* password to try first -- since each file carries its
    own salt (crypto.py), sessions aren't forced to share one password.
    Callers reading an encrypted file should fall back to prompting for a
    session-specific one on failure rather than treating this as final;
    see compress.py's decrypt_file_with_retry."""
    env = os.environ.get("KORECORD_PASSWORD")
    if env:
        return env
    stored = encryption_stored_password()
    if stored:
        return stored
    if prompt_if_missing and sys.stdin.isatty():
        return getpass.getpass("korec: encryption password: ")
    return None
