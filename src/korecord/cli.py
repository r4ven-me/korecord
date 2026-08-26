from __future__ import annotations

import argparse
import getpass
import os
import signal
import sys
from pathlib import Path

from . import config, db, recorder, render


def cmd_record(args: argparse.Namespace) -> None:
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    sys.exit(recorder.record(command, label=args.label))


def cmd_ls(args: argparse.Namespace) -> None:
    db.print_sessions(limit=args.limit, host=args.host, since=args.since)


def cmd_grep(args: argparse.Namespace) -> None:
    found = db.grep_sessions(
        args.pattern, regex=args.regex, max_lines=args.max_lines, session_id=args.session
    )
    sys.exit(0 if found else 1)


def cmd_play(args: argparse.Namespace) -> None:
    recorder.play(args.id)


def cmd_cat(args: argparse.Namespace) -> None:
    db.print_transcript(args.id)


def cmd_show(args: argparse.Namespace) -> None:
    db.print_session(args.id)


def cmd_decrypt(args: argparse.Namespace) -> None:
    db.decrypt_session(args.id)


def cmd_encrypt(args: argparse.Namespace) -> None:
    db.encrypt_session(args.id)


def cmd_render_internal(args: argparse.Namespace) -> None:
    # Whether *this* session is encrypted comes from `--encrypted` (set by
    # `record()` from the session's own `encrypted` flag), not from
    # whether a password happens to be resolvable -- $KORECORD_PASSWORD
    # could be sitting in the ambient environment for unrelated reasons,
    # and using that alone would risk wrongly encrypting a plain session's
    # transcript. The password itself, when it *is* needed, travels the
    # same way: via $KORECORD_PASSWORD (the same var resolve_password()
    # already checks first anywhere else) rather than argv, so it doesn't
    # show up in `ps`/process listings. prompt_if_missing=False because
    # this runs detached with no stdin to prompt on anyway.
    password = config.resolve_password(prompt_if_missing=False) if args.encrypted else None
    render.render_cast_to_text(Path(args.cast), Path(args.txt), password=password)


def cmd_config_show(args: argparse.Namespace) -> None:
    print(f"config file:  {config.config_file()}")
    print(f"data dir:     {config.data_dir()}")
    print(f"index db:     {config.db_path()}")
    if os.environ.get("KORECORD_DATA_DIR"):
        print("(data dir is currently overridden by $KORECORD_DATA_DIR)")


def cmd_config_set_data_dir(args: argparse.Namespace) -> None:
    config.set_data_dir(args.path)
    print(f"data dir set to: {config.data_dir()}")
    print("(existing recordings are not moved -- move them yourself if needed)")


def cmd_config_unset_data_dir(args: argparse.Namespace) -> None:
    config.set_data_dir(None)
    print(f"data dir reset to default: {config.data_dir()}")


def cmd_config_encryption_show(args: argparse.Namespace) -> None:
    enabled = config.encryption_enabled()
    print(f"encryption: {'enabled' if enabled else 'disabled'} (for future recordings)")
    stored = config.encryption_stored_password() is not None
    print(f"password stored in config file: {'yes' if stored else 'no'}")
    if not stored:
        print("(korec will use $KORECORD_PASSWORD or prompt interactively when a password is needed)")


def cmd_config_encryption_enable(args: argparse.Namespace) -> None:
    env_password = os.environ.get("KORECORD_PASSWORD")
    if sys.stdin.isatty():
        password = getpass.getpass("New encryption password: ")
        if not password:
            sys.exit("korec: empty password refused")
        if getpass.getpass("Confirm password: ") != password:
            sys.exit("korec: passwords didn't match")
    elif env_password:
        password = env_password
    else:
        sys.exit("korec: no tty to prompt on, and $KORECORD_PASSWORD isn't set")

    store = args.store_password
    if store is None:
        if sys.stdin.isatty():
            answer = input(
                f"Store this password in the config file ({config.config_file()})? "
                "Storing it means korec never has to ask again, but anyone who can "
                "read that file can decrypt your recordings. [y/N]: "
            ).strip().lower()
            store = answer in ("y", "yes")
        else:
            store = False

    config.set_encryption(enabled=True, password=password, store_password=store)

    print("korec: encryption enabled for future recordings.")
    print(
        f"Password stored in {config.config_file()} (mode 0600)."
        if store else
        "Password NOT stored -- set $KORECORD_PASSWORD or korec will prompt interactively each time."
    )


def cmd_config_encryption_disable(args: argparse.Namespace) -> None:
    config.set_encryption(enabled=False)
    print("korec: encryption disabled for future recordings.")
    print("(existing encrypted sessions are unaffected and still need the password to read)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="korec",
        description="Permanent, compressed, searchable recording of terminal sessions.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser(
        "record",
        help="Record a command's session and index it (e.g. `korec record -- ssh myhost`)",
    )
    pr.add_argument("--label", help="Override the auto-derived session label (default: derived from the command, e.g. the ssh target host)")
    pr.add_argument("command", nargs=argparse.REMAINDER, help="Command to run and record")
    pr.set_defaults(func=cmd_record)

    pl = sub.add_parser("ls", help="List recorded sessions, newest first")
    pl.add_argument("--limit", type=int, default=50)
    pl.add_argument("--host", help="Filter by label/host substring")
    pl.add_argument("--since", help="Only sessions starting at/after this ISO timestamp")
    pl.set_defaults(func=cmd_ls)

    pg = sub.add_parser("grep", help="Full-text search across recorded sessions")
    pg.add_argument("pattern")
    pg.add_argument("--session", type=int, help="Search only this session id (from `korec ls`), instead of every session")
    pg.add_argument("--regex", action="store_true", help="Treat pattern as a regular expression")
    pg.add_argument("--max-lines", type=int, default=5, help="Max matching lines to show per session")
    pg.set_defaults(func=cmd_grep)

    pp = sub.add_parser("play", help="Replay a recorded session")
    pp.add_argument("id", type=int)
    pp.set_defaults(func=cmd_play)

    pcat = sub.add_parser("cat", help="Print a session's timestamped transcript (pipe into less to browse it)")
    pcat.add_argument("id", type=int)
    pcat.set_defaults(func=cmd_cat)

    ps = sub.add_parser("show", help="Show metadata for one session")
    ps.add_argument("id", type=int)
    ps.set_defaults(func=cmd_show)

    pdecrypt = sub.add_parser(
        "decrypt", help="Permanently decrypt one session's stored files in place (no more password needed for it)"
    )
    pdecrypt.add_argument("id", type=int)
    pdecrypt.set_defaults(func=cmd_decrypt)

    pencrypt = sub.add_parser(
        "encrypt", help="Permanently encrypt one currently-plain session's stored files in place"
    )
    pencrypt.add_argument("id", type=int)
    pencrypt.set_defaults(func=cmd_encrypt)

    pc = sub.add_parser("config", help="View or change where recordings are stored")
    csub = pc.add_subparsers(dest="config_cmd", required=True)

    csub.add_parser("show", help="Show the active config file and data directory").set_defaults(func=cmd_config_show)

    cset = csub.add_parser("set-data-dir", help="Persist a custom directory for recordings and the index DB")
    cset.add_argument("path", help="New storage directory (created if missing)")
    cset.set_defaults(func=cmd_config_set_data_dir)

    csub.add_parser("unset-data-dir", help="Revert to the default storage directory").set_defaults(func=cmd_config_unset_data_dir)

    penc = csub.add_parser("encryption", help="Manage optional password-based encryption of recordings")
    encsub = penc.add_subparsers(dest="encryption_cmd", required=True)

    encsub.add_parser(
        "show", help="Show whether encryption is on and whether a password is stored"
    ).set_defaults(func=cmd_config_encryption_show)

    penc_enable = encsub.add_parser("enable", help="Turn on encryption for future recordings")
    penc_enable.add_argument(
        "--store-password", action=argparse.BooleanOptionalAction, default=None,
        help="Store the password in the config file so korec never has to ask again "
             "(anyone who can read that file can then decrypt your recordings). "
             "Default: ask interactively, or don't store when running non-interactively.",
    )
    penc_enable.set_defaults(func=cmd_config_encryption_enable)

    encsub.add_parser(
        "disable", help="Turn off encryption for future recordings (existing encrypted sessions are unaffected)"
    ).set_defaults(func=cmd_config_encryption_disable)

    # Internal: invoked by `record` as a detached background process to
    # build the searchable transcript sidecar. Not meant for direct use.
    pi = sub.add_parser("_render", help=argparse.SUPPRESS)
    pi.add_argument("cast")
    pi.add_argument("txt")
    pi.add_argument("--encrypted", action="store_true")
    pi.set_defaults(func=cmd_render_internal)

    return p


def main(argv: list[str] | None = None) -> None:
    # Python turns SIGPIPE into a catchable BrokenPipeError instead of the
    # default kill-the-process behavior every other Unix CLI tool gets for
    # free -- without this, quitting `less`/`head` before EOF (e.g. `korec
    # cat 17 | less`, then `q`) prints an ugly traceback the moment we next
    # write to the now-closed pipe. Restoring the default disposition makes
    # us die the same quiet way `cat`/`grep` do.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
