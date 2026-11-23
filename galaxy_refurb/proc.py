"""Run a command and stream its output line by line (progress bars use \\r, so split on both)."""
from __future__ import annotations

import os
import subprocess
import threading
from collections import deque

from .log import log


def run_stream(cmd: list[str], on_line=None, timeout: float | None = None,
               keep: int = 60) -> tuple[int, list[str]]:
    """Run cmd, calling on_line(text) for each output line. Returns (exit_code, last `keep` lines)."""
    log("$ " + " ".join(cmd))
    tail: deque[str] = deque(maxlen=keep)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as exc:
        msg = f"could not start {cmd[0]}: {exc}"
        log(msg)
        return 127, [msg]

    timer = None
    if timeout:
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
    buf = b""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    last = ""

    def emit(raw: bytes) -> None:
        nonlocal last
        text = raw.decode("utf-8", errors="replace").strip()
        if not text or text == last:
            return
        last = text
        tail.append(text)
        log(text)
        if on_line:
            on_line(text)

    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk
        while True:
            idx = min((i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0), default=-1)
            if idx < 0:
                break
            emit(buf[:idx])
            buf = buf[idx + 1:]
    if buf:
        emit(buf)
    code = proc.wait()
    if timer:
        timer.cancel()
    log(f"exit {code}")
    return code, list(tail)
