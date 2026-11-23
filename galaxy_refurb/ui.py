"""Console prompts. Uses `rich` for colour when available, plain text otherwise."""
from __future__ import annotations

from .log import log

try:
    from rich.console import Console
    from rich.panel import Panel
    _console: Console | None = Console()
except ImportError:  # pragma: no cover
    _console = None


def _print(text: str, style: str = "") -> None:
    log(text)
    if _console:
        _console.print(text, style=style, markup=False, highlight=False)
    else:
        print(text)


def step(text: str) -> None:
    if _console:
        log(f"== {text}")
        _console.print(Panel(text, style="bold cyan"))
    else:
        _print(f"\n=== {text} ===")


def info(text: str) -> None:
    _print(f"  {text}")


def ok(text: str) -> None:
    _print(f"  OK  {text}", "bold green")


def warn(text: str) -> None:
    _print(f"  !!  {text}", "bold yellow")


def error(text: str) -> None:
    _print(f"  XX  {text}", "bold red")


def instruct(lines: list[str]) -> None:
    """Show numbered steps for the person at the desk, then wait for Enter."""
    _print("")
    for i, line in enumerate(lines, 1):
        _print(f"  {i}. {line}", "bold white")
    input("\n  Press Enter when done... ")
    log("(user pressed Enter)")


def confirm(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        ans = input(f"\n  {question} [{hint}] ").strip().lower()
        log(f"Q: {question} -> {ans!r}")
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"\n  {question}{suffix}: ").strip()
    log(f"Q: {question} -> {ans!r}")
    return ans or default


def table(title: str, rows: list[tuple[str, str]]) -> None:
    if _console:
        from rich.table import Table
        t = Table(title=title, show_header=False)
        t.add_column(style="bold")
        t.add_column()
        for k, v in rows:
            t.add_row(k, v)
        _console.print(t)
    else:
        _print(title)
        for k, v in rows:
            _print(f"  {k:<18} {v}")
    for k, v in rows:
        log(f"{k}: {v}")
