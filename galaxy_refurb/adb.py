"""Thin wrapper around adb.exe."""
from __future__ import annotations

import re
import subprocess
import time

from .log import log


class AdbError(RuntimeError):
    pass


def parse_getprop(text: str) -> dict[str, str]:
    """Parse `adb shell getprop` output: lines like `[ro.product.model]: [SM-S921B]`."""
    props: dict[str, str] = {}
    for m in re.finditer(r"^\[([^\]]+)\]:\s*\[(.*)\]\s*$", text, re.MULTILINE):
        props[m.group(1)] = m.group(2)
    return props


def parse_devices(text: str) -> list[tuple[str, str]]:
    """Parse `adb devices` output into (serial, state) pairs."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


class Adb:
    def __init__(self, exe: str):
        self.exe = exe

    def run(self, *args: str, serial: str | None = None, timeout: int = 60, check: bool = True) -> str:
        cmd = [self.exe]
        if serial:
            cmd += ["-s", serial]
        cmd += list(args)
        log(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb timed out: {' '.join(args)}") from exc
        except OSError as exc:
            raise AdbError(f"could not run adb: {exc}") from exc
        if check and proc.returncode != 0:
            raise AdbError(f"adb failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def devices(self) -> list[tuple[str, str]]:
        return parse_devices(self.run("devices"))

    def shell(self, serial: str, command: str, timeout: int = 60) -> str:
        return self.run("shell", command, serial=serial, timeout=timeout, check=False)

    def getprop(self, serial: str) -> dict[str, str]:
        return parse_getprop(self.shell(serial, "getprop"))

    def wait_for_device(self, timeout_s: float, poll_s: float = 2.0) -> tuple[str, str] | None:
        """Return the first (serial, state) seen within timeout, else None."""
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                devs = self.devices()
            except AdbError:
                devs = []
            if devs:
                return devs[0]
            time.sleep(poll_s)
        return None

    def reboot_download(self, serial: str) -> None:
        # Samsung bootloaders accept "download" as a reboot target.
        self.run("reboot", "download", serial=serial, check=False, timeout=30)
