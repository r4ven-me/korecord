"""Shared terminal-output helpers.

Every "show status" style command (`korec ls`, `korec show`, `korec config
show`/`encryption show`/`compression show`) renders two ways off the same
detection: colored and boxed (rich) when actually connected to a terminal,
or a plain, colorless, fixed-format fallback otherwise (piped, redirected,
captured by a test) -- so a script parsing korec's output never has to deal
with ANSI codes or box-drawing characters, and existing output shapes don't
change for it. `console.is_terminal` is what every caller branches on."""
from __future__ import annotations

from rich import box
from rich.console import Console
from rich.table import Table

console = Console()


def print_kv_table(rows: list[tuple[str, object]]) -> None:
    """A header-less two-column table for `key: value`-shaped output --
    used once `console.is_terminal` is already known True. `rows` values
    may be plain strings or rich renderables (e.g. `Text` with a style)."""
    table = Table(show_header=False, box=box.ROUNDED, border_style="grey50")
    table.add_column(style="bold")
    table.add_column()
    for key, value in rows:
        table.add_row(key, value)
    console.print(table)
