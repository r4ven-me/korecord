# korecord

Permanent, compressed, searchable recording of terminal sessions (ssh and
beyond), built on top of [asciinema](https://asciinema.org/).

- Compresses every recording to a zstd `.cast.zst` file.
- Builds a searchable plaintext transcript per session by replaying the
  recording through a real terminal emulator ([pyte](https://github.com/selectel/pyte)),
  so redraws (shell autosuggestions, fancy prompts, progress bars) collapse
  to what was actually on screen instead of leaving garbled duplicate lines.
- Indexes every session (start/end time, host, tty, size, exit code) in a
  local SQLite database for fast listing and filtering.
- `korec grep`/`korec cat` work on a still-running session too, live off
  whatever's been captured so far -- not just finished recordings.
- Optional password-based encryption at rest (off by default) -- see
  [Optional encryption](#optional-encryption).

## Requirements

korec shells out to the [asciinema](https://asciinema.org/) CLI -- **3.x**
specifically (the Rust rewrite; run `asciinema --version` to check). It has
to be installed separately and be on `$PATH`, via whichever of these is
more convenient:

```sh
# prebuilt binary -- see https://github.com/asciinema/asciinema/releases
# for other platforms
curl -Lo ~/.local/bin/asciinema \
  https://github.com/asciinema/asciinema/releases/latest/download/asciinema-x86_64-unknown-linux-gnu
chmod +x ~/.local/bin/asciinema

# or, if you have a Rust toolchain: builds from source via crates.io,
# installs to ~/.cargo/bin
cargo install asciinema
```

asciinema 3.x is a standalone binary, not published to PyPI (the old
`pip install asciinema` line is frozen at 2.4.0, the last release before
the rewrite, and doesn't work with korec) -- so it can't be pulled in as an
ordinary Python dependency the way `pyte`/`zstandard` are. `korec record`
checks the version it finds on `$PATH` and refuses with a clear error
rather than silently misbehaving if it's the old 2.x line.

## Install

```sh
pipx install korecord
# or, from a local checkout:
pipx install /path/to/korecord
```

This installs a single command, `korec`. Don't forget asciinema itself --
see [Requirements](#requirements) above.

## Usage

```sh
# record any command's session -- ssh is just the common case
korec record -- ssh myhost

# override the auto-derived session label (default: the ssh target host,
# or the program name for anything else)
korec record --label prod-db -- ssh myhost

# list recorded sessions, newest first
korec ls
korec ls --host myhost --limit 20

# full-text search across every recorded session
korec grep "systemctl restart"
korec grep --regex 'error|failed'

# search within just one session (id from `korec ls`)
korec grep --session 42 "systemctl restart"

# print a session's full timestamped transcript -- e.g. pipe into less,
# find the session/time with `korec grep` first, then jump to it here
korec cat 42 | less

# replay a session by id (from `korec ls`)
korec play 42

# show raw metadata for one session
korec show 42

# view or change where recordings are stored (see below)
korec config show
```

## What gets saved, and where

By default everything lives under `~/.local/share/korecord` (or
`$XDG_DATA_HOME/korecord` if that's set) -- see [Changing the storage
location](#changing-the-storage-location) to point it somewhere else.

```
<data-dir>/
  <label>/
    <year>/
      <month>/
        <timestamp>_<tty>_<pid>.cast.zst[.enc]   # full asciicast recording, zstd-compressed
        <timestamp>_<tty>_<pid>.txt.zst[.enc]    # searchable plaintext transcript, zstd-compressed
  index.db                                   # SQLite index of every session
```

`.enc` only appears when [encryption](#optional-encryption) was on for that
particular recording.

- **`<label>`** is the recording's grouping key -- by default the ssh
  target host (`ssh myhost` -> `myhost`), or the program name for any other
  command; override it with `korec record --label NAME -- ...`.
- **`<timestamp>_<tty>_<pid>`** is `YYYY-MM-DD_HHMMSS_<tty-name>_<pid>`,
  e.g. `2026-08-17_224255_pts_3_48213`, so filenames sort chronologically
  and the pid guarantees no collision even if two sessions somehow start in
  the same second with no distinguishable tty.
- **Exactly two files per session, once it's finished** -- `*.cast.zst` and
  `*.txt.zst` -- regardless of how long the session runs or how much
  output it produces. There's no per-hour or per-block splitting: a
  session that runs for 10 seconds and one that runs for 20 hours both
  produce one `.cast.zst` and one `.txt.zst`.
- **`*.cast.zst`** is the raw [asciicast v2](https://docs.asciinema.org/manual/asciicast/v2/)
  recording (every byte of terminal output plus timestamps), zstd-compressed.
  This is what `korec play <id>` replays. asciinema itself writes straight
  to a plain, uncompressed file as the session runs (a real file is
  required -- there's no more streaming to stdout, see
  [crash safety](#long-running-sessions-and-crash-safety)); once the
  session ends, `korec` compresses that file in one pass into `.cast.zst`
  and deletes the plain copy.
- **`*.txt.zst` does not exist until the session ends**, and is built in
  one pass by replaying the finished recording through a real terminal
  emulator (`pyte`) and dumping the final screen contents -- rendering
  happens in a detached background process right after the session ends
  (so closing the terminal doesn't cut it off). That said, `korec
  grep`/`korec cat` don't just wait for it: for a still-running session
  neither file exists yet either, so both render on the fly, straight from
  whatever asciinema has flushed to the plain (uncompressed) recording so
  far -- a live session is fully searchable, just slower than reading the
  pre-built sidecar.
- **`index.db`** is a small SQLite database with one row per session:
  start/end time, duration, local host, label, tty, `.cast.zst` size, and
  the recorded command's exit code. `korec ls` and `korec show <id>` read
  from it; it's what makes sessions easy to find without opening every
  recording. A row is written the moment recording *starts*, not when it
  ends, so a still-running session already shows up in `korec ls` (as
  `STATUS RUNNING`) instead of only appearing after you disconnect.

Recordings are kept forever by default -- nothing is ever deleted or
rotated automatically. Typical size for an interactive session, after
compression, is well under a megabyte per hour; something that streams
continuous output (e.g. `tail -f` on a busy log) will be larger, since
`korec` compresses what actually happened rather than trimming it.

## Long-running sessions and crash safety

A recording can run for as long as the underlying command does -- hours or
days -- without special handling:

- **`korec record` itself stays low-memory while recording.** asciinema (a
  separate process) does the actual pty capture and writes straight to
  disk; `korec` is just waiting on it. Disk only grows with actual
  output -- idle time costs nothing, there's nothing to write when nothing
  happened.
- **The index row exists from the start**, so `korec ls` can show a session
  that's still going (`STATUS RUNNING`), not just ones that already ended.

The one real risk with a long recording is a hard failure -- `kill -9`,
a crashed machine, a power cut -- partway through. Two things limit the
damage:

- asciinema flushes each event to the plain recording file as it happens,
  not in large buffered batches, so a hard kill mid-recording loses at most
  whatever hasn't hit disk yet -- typically a fraction of a second's worth
  of output. That not-yet-compressed file is exactly what `korec grep
  --session <id>` and `korec cat <id>` read directly for a session that's
  still running (or was abandoned): nothing is lost until `korec` actually
  gets around to compressing it, and if `korec record`'s own process dies
  before that step, the plain file just sits there, still fully readable
  the same way.
- If `korec record`'s own process dies without a clean exit, its index row
  is left with no `end_time`. `korec ls`/`korec show` don't just guess --
  they check whether the process's recorded `pid` is still alive and report
  `STATUS RUNNING` or `STATUS KILLED?` accordingly, so an abandoned session
  is visible as such rather than silently missing.
- A closed terminal tab, a crashed window manager, or anything else that
  sends `korec record` a `SIGTERM`/`SIGHUP` (as opposed to an uncatchable
  `kill -9`) no longer leaves the recording running as an orphan. asciinema
  deliberately ignores those signals itself, so korec catches them, kills
  asciinema and its whole process tree (including anything it's running,
  like a nested `ssh`), and finishes the session normally -- it's finalized
  with a real duration and an exit code reflecting the kill, instead of
  surviving indefinitely with no controlling terminal attached to it.

Two costs scale with session length. First, right after a session ends,
`korec` reads the whole plain recording into memory to compress it in one
shot (then deletes the plain copy) -- for ordinary interactive use that's
negligible, but a session with sustained heavy output (`tail -f` on a busy
log, `htop` left running for a day) can make that a real amount of memory,
however briefly. Second, and larger: the `.txt.zst` transcript is built by
fully replaying the session through `pyte`, a pure-Python terminal
emulator, in one pass after the session ends. That's fast for ordinary
interactive use, but a session like that can reach hundreds of thousands of
lines, and rendering that can take minutes and hold a non-trivial amount of
memory while it runs. Neither blocks the recording itself or the
terminal -- transcript rendering in particular is a detached background
process -- and `korec grep`/`korec cat` can still search/replay that
session live (see above) while the sidecar isn't ready yet. Scrollback
beyond 5,000,000 lines in a single session is dropped from the transcript
(oldest first) as a memory safety valve; `korec play` is unaffected, since
it replays the original recording, not the transcript.

## Changing the storage location

```sh
# show the active config file, data directory, and index DB path
korec config show

# persist a new storage location (survives across shells/reboots)
korec config set-data-dir /mnt/bigdisk/korecord

# revert to the default
korec config unset-data-dir
```

This is the recommended way to relocate storage, because `korec` is
typically launched as a terminal profile's *custom command* (see below),
which execs the binary directly rather than through a login shell -- so an
env var set in `.bashrc`/`.zshrc` would never actually be seen. The setting
is written to `~/.config/korecord/config.json` (or `$XDG_CONFIG_HOME/korecord/config.json`).

For one-off overrides (scripts, tests, CI) the `$KORECORD_DATA_DIR`
environment variable takes precedence over the config file when set.

Note that changing the location does not move existing recordings --
move the directory yourself first if you want to keep history contiguous:

```sh
mv ~/.local/share/korecord /mnt/bigdisk/korecord
korec config set-data-dir /mnt/bigdisk/korecord
```

## Optional encryption

```sh
# turn on encryption for future recordings -- prompts for a password,
# then asks whether to save it in the config file
korec config encryption enable

# same, non-interactively (for scripting/CI): reads $KORECORD_PASSWORD
# instead of prompting, and --store-password decides whether to save it
KORECORD_PASSWORD=hunters2 korec config encryption enable --store-password

# check status (never prints the password itself)
korec config encryption show

# turn off for future recordings -- existing encrypted sessions are
# unaffected and still need the password to read
korec config encryption disable

# flip one *existing* session's encryption, in place, regardless of the
# current on/off setting above
korec decrypt 42   # -> plain, no password needed for it ever again
korec encrypt 42   # -> encrypted with the currently configured password
```

Off by default. When on, both a session's `.cast.zst` and `.txt.zst` are
encrypted (AES-256-GCM, key derived from the password via scrypt) --
`korec record`/`grep`/`cat`/`play` all handle it transparently, prompting
for the password if it isn't available some other way. `korec config
encryption enable`/`disable` only decide what *future* recordings do;
`korec decrypt <id>`/`korec encrypt <id>` change one already-recorded
session directly -- rewriting its `.cast.zst[.enc]`/`.txt.zst[.enc]` files
in place and flipping its `encrypted` flag in the index (visible as the
`ENC` column in `korec ls`).

**The password alone is always enough to decrypt -- nothing else to keep
track of or back up.** Every encrypted file carries its own randomly
-generated salt embedded directly in it; there's no shared salt living in
the config file that a lost/corrupted config could ever orphan data
against. This wasn't the original design: an earlier version kept one
salt in `config.json`, which seemed like the natural place for it -- until
that file got lost separately from the data, and re-entering the exact
right password still couldn't decrypt anything, because a *different*
salt came back in its place (a salt has to match precisely what encrypted
the data). Embedding it per-file removes the possibility of that
happening again, and as a side effect means different sessions are free
to use different passwords -- `korec` just asks again if the one it has
on hand doesn't open a particular one, rather than giving up. There's
still no password-*change* support across many sessions at once, though:
`encrypt`/`decrypt` always use whatever password is currently
configured/resolvable for that one session, so there's no bulk migration
if you want to move everything to a new password.

**Threat model, and why the config file is allowed to hold the password at
all:** this protects a *copy* of the data directory that ends up separated
from the config file -- a backup, a synced folder, an old disk -- not
against a local attacker who already has filesystem access to both (which,
for most setups, means the same access to your $HOME either way). That's a
deliberate tradeoff for convenience within that threat model, not a
claim that storing the password is safe in general -- if it doesn't fit
yours, use `--no-store-password` (or just say no at the prompt) and rely on
`$KORECORD_PASSWORD` or an interactive prompt instead.

A password is only ever needed to read the *rendered artifacts* of a
finished session (`.cast.zst.enc`/`.txt.zst.enc` -- an encrypted session's
files get an extra `.enc` on top of the usual `.cast.zst`/`.txt.zst`, so
they're identifiable straight from a file listing without consulting the
index). A session that's still recording reads from the plain file
asciinema itself writes live, which is never encrypted regardless of this
setting -- asciinema has no idea korec might encrypt the finished result --
so `korec grep`/`cat` on a live session never need a password, encrypted or
not.

`korec record` resolves the password once, up front, in the foreground
(where it's safe to prompt); the detached background process that builds
the `.txt.zst.enc` transcript gets that same password passed via an
environment variable, never via a command-line argument (which would leak
into `ps`/process listings).

### Decrypting without korecord

[`scripts/decrypt-recording.py`](scripts/decrypt-recording.py) decrypts a
`.cast.zst.enc`/`.txt.zst.enc` file on its own -- it doesn't import or
require korecord at all, only the `cryptography` package (`pip install
cryptography`) and a `zstd` binary on `$PATH`. Useful if korecord itself is
ever broken, gone, or unavailable and you just need the data back:

```sh
python3 scripts/decrypt-recording.py session.txt.zst.enc
# prompts for the password (or reads $KORECORD_PASSWORD); writes
# session.txt next to it by default -- nothing else needed
```

The file format itself (documented at the top of that script, and in
[`crypto.py`](src/korecord/crypto.py)) is nothing exotic: a random 16-byte
salt and 12-byte nonce followed by AES-256-GCM ciphertext, keyed by
`scrypt(password, salt, n=2**14, r=8, p=1, dklen=32)`, wrapping ordinary
zstd-compressed data -- reproducible with any standard crypto library if
you'd rather not use the provided script.

## Terminal-emulator integration (e.g. Tilix)

Point a terminal profile's "custom command" at `korec record -- ssh
myhost` instead of a bare `ssh myhost`. Every tab/window opened from that
profile then records itself automatically.

A custom command like this execs directly, inheriting whatever bare `$PATH`
the desktop/window-manager session set up -- not the fuller one an
interactive shell builds via `.bashrc`/`.zshrc` (which never runs here, see
[Changing the storage location](#changing-the-storage-location) above for
the same gotcha with env vars). If `asciinema` was installed to
`~/.local/bin` or `~/.cargo/bin` (see [Requirements](#requirements)), korec
finds it there even when it's missing from that bare `$PATH`; anywhere
else, make sure `$PATH` for your desktop session actually includes it.

## Releasing

Releases are published to PyPI automatically by
[`.github/workflows/publish.yml`](.github/workflows/publish.yml) whenever a
`vX.Y.Z` tag is pushed:

```sh
# bump the version in pyproject.toml first, then:
git tag v0.2.0
git push origin v0.2.0
```

The workflow builds the sdist/wheel, checks the tag matches
`pyproject.toml`'s `version`, installs the built wheel into a throwaway venv
as a smoke test, and only then publishes -- via
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC),
so no PyPI API token is stored as a repo secret. One-time setup required
before the first release:

1. On PyPI, under `korecord` -> *Publishing* (or, for a project that
   doesn't exist yet, [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)
   to add a *pending* trusted publisher), add a trusted publisher with:
   - Owner: `r4ven-me`
   - Repository name: `korecord`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. In the GitHub repo, under *Settings -> Environments*, create an
   environment named `pypi` (optionally with protection rules, e.g.
   restricting it to tag pushes).
