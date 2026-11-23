"""Watch the USB port: what's plugged in right now?

States (shown on the home screen):
  none              nothing plugged in
  samsung_no_debug  a Samsung phone is plugged in but USB debugging is off (Windows only)
  no_driver         a Samsung phone is plugged in but the Samsung USB driver is missing (Windows only)
  unauthorized      USB debugging on, waiting for "Allow" on the phone
  adb               ready - phone readable over USB debugging
  download          phone is in Download mode
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass

from .adb import Adb, AdbError
from .config import IS_WINDOWS, Config
from .flash import Samloader

SAMSUNG_VID = "04E8"


@dataclass
class DeviceStatus:
    state: str = "none"
    model: str = ""
    serial: str = ""
    label: str = "Plug in a phone"
    hint: str = "Use a USB data cable. Unlock the phone."

    def to_dict(self) -> dict:
        return asdict(self)


def parse_pnp_json(text: str) -> list[dict]:
    """Parse output of the PowerShell Samsung USB device query."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def windows_samsung_usb() -> list[dict]:
    """Samsung USB devices currently plugged in (Windows), with driver error codes."""
    if not IS_WINDOWS:
        return []
    ps = ("Get-CimInstance Win32_PnPEntity -Filter \"PNPDeviceID LIKE 'USB\\\\VID_04E8%'\" | "
          "Select-Object Name,ConfigManagerErrorCode | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_pnp_json(out)


def classify_usb(entries: list[dict]) -> DeviceStatus | None:
    if not entries:
        return None
    if any(int(e.get("ConfigManagerErrorCode") or 0) != 0 for e in entries):
        return DeviceStatus("no_driver", label="Samsung phone found - driver missing",
                            hint="Install the Samsung USB driver (see the help link), then replug the phone.")
    return DeviceStatus("samsung_no_debug", label="Samsung phone connected",
                        hint="USB debugging is off - press Start and we'll show you how to turn it on.")


def current_status(cfg: Config, adb: Adb | None, check_usb: bool = True,
                   model_cache: dict | None = None) -> DeviceStatus:
    cache = model_cache if model_cache is not None else {}
    if adb:
        try:
            devs = adb.devices()
        except AdbError:
            devs = []
        for serial, state in devs:
            if state == "device":
                if serial not in cache:
                    cache[serial] = adb.shell(serial, "getprop ro.product.model").strip()
                model = cache[serial]
                return DeviceStatus("adb", model=model, serial=serial, label=model or "Phone connected",
                                    hint="Ready. Press Start.")
        for serial, state in devs:
            if state == "unauthorized":
                return DeviceStatus("unauthorized", serial=serial, label="Tap 'Allow' on the phone",
                                    hint="A box on the phone asks to allow USB debugging. Tick 'Always allow' and tap Allow.")
    if cfg.samloader:
        info = Samloader(cfg.samloader).detect()
        if info:
            return DeviceStatus("download", model=info.model, serial=info.serial,
                                label=info.model or "Phone in Download mode", hint="Ready. Press Start.")
    if check_usb:
        st = classify_usb(windows_samsung_usb())
        if st:
            return st
    return DeviceStatus()


class DeviceWatcher(threading.Thread):
    """Polls the USB port while no job is running and pushes changes to the session."""

    def __init__(self, cfg: Config, adb: Adb | None, on_change, interval: float = 2.0):
        super().__init__(daemon=True)
        self.cfg, self.adb, self.on_change, self.interval = cfg, adb, on_change, interval
        self._paused = threading.Event()
        self._stop = threading.Event()
        self.status = DeviceStatus()
        self._cache: dict = {}
        self._tick = 0

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if not self._paused.is_set():
                self._tick += 1
                try:
                    st = current_status(self.cfg, self.adb, check_usb=(self._tick % 2 == 0), model_cache=self._cache)
                except Exception:  # never let the watcher die
                    st = DeviceStatus()
                # USB scan only every other tick: keep the last USB-only state in between.
                if self._tick % 2 == 1 and st.state == "none" and self.status.state in ("samsung_no_debug", "no_driver"):
                    st = self.status
                if st != self.status:
                    self.status = st
                    self.on_change(st)
            time.sleep(self.interval)
