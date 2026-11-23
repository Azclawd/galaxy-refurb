"""Per-phone log file. Every command the tool runs is written here so problems can be diagnosed."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

_log_path: Path | None = None


def start_log(logs_dir: Path, name: str) -> Path:
    global _log_path
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    _log_path = logs_dir / f"{stamp}-{safe}.log"
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    return _log_path


def log(message: str) -> None:
    if _log_path is None:
        return
    stamp = _dt.datetime.now().strftime("%H:%M:%S")
    with _log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def current_log() -> Path | None:
    return _log_path
