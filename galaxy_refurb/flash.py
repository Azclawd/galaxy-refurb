"""Put official firmware on the phone.

Automatic: samloader-rs (https://github.com/topjohnwu/samloader-rs) - an open-source
           Odin-protocol flasher derived from Heimdall. On Windows it uses Samsung's own
           USB driver (virtual COM port), the same driver Odin uses, so nothing needs swapping.
Fallback:  Odin, guided step by step.

Flashing BL + AP + CP + CSC (not HOME_CSC) also wipes the phone - that's the factory reset.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .adb import Adb
from .config import IS_WINDOWS, Config
from .log import log


@dataclass
class DownloadModeInfo:
    model: str = ""
    serial: str = ""
    sales_code: str = ""
    bootloader: str = ""
    storage: str = ""


def parse_detect(text: str) -> DownloadModeInfo:
    """Parse `samloader detect` output (download-mode device info)."""
    def field(label: str) -> str:
        m = re.search(r"^\s*" + re.escape(label) + r":\s*(.+?)\s*$", text, re.MULTILINE)
        return m.group(1) if m else ""
    return DownloadModeInfo(model=field("Model"), serial=field("Serial Number"),
                            sales_code=field("Sales Code"), bootloader=field("Bootloader Version"),
                            storage=field("Storage"))


def flash_command(samloader: str, tars: dict[str, Path]) -> list[str]:
    cmd = [samloader, "flash", "--wait"]
    for comp in ("BL", "AP", "CP", "CSC"):
        if comp in tars:
            cmd += [f"--{comp}", str(tars[comp])]
    return cmd


class Samloader:
    def __init__(self, exe: str):
        self.exe = exe

    def detect(self) -> DownloadModeInfo | None:
        try:
            proc = subprocess.run([self.exe, "detect"], capture_output=True, text=True, timeout=30,
                                  encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"detect failed: {exc!r}")
            return None
        log(f"$ samloader detect -> {proc.returncode}\n{proc.stdout}{proc.stderr}")
        return parse_detect(proc.stdout) if proc.returncode == 0 else None

    def wait_detect(self, seconds: float) -> DownloadModeInfo | None:
        end = time.time() + seconds
        while time.time() < end:
            info = self.detect()
            if info:
                return info
            time.sleep(3)
        return None

    def flash(self, tars: dict[str, Path], echo) -> int:
        cmd = flash_command(self.exe, tars)
        log("$ " + " ".join(cmd))
        # Let samloader draw its own progress bar straight to the console.
        proc = subprocess.run(cmd)
        log(f"exit {proc.returncode}")
        return proc.returncode


def enter_download_mode_instructions() -> list[str]:
    return [
        "Turn the phone OFF (unplugged).",
        "Hold Volume UP + Volume DOWN together and plug in the USB cable (keep holding).",
        "When the blue warning screen appears, press Volume UP once to continue.",
        "You should now see a blue screen saying 'Downloading... Do not turn off target'.",
    ]


def get_into_download_mode(cfg: Config, adb: Adb | None, serial: str | None, ui) -> DownloadModeInfo | None:
    if not cfg.samloader:
        return None
    sl = Samloader(cfg.samloader)
    info = sl.detect()
    if info:
        return info
    if adb and serial:
        ui.info("Rebooting phone into Download mode...")
        adb.reboot_download(serial)
    else:
        ui.instruct(enter_download_mode_instructions())
    wait_s = cfg.settings.get("download_mode_wait_seconds", 90)
    info = sl.wait_detect(wait_s)
    while not info:
        if not ui.confirm("Can't see the phone in Download mode. Try again?"):
            return None
        ui.instruct(enter_download_mode_instructions()
                    + ["If it still isn't found: try another USB cable/port, and make sure the "
                       "Samsung USB driver is installed (see README)."])
        info = sl.wait_detect(wait_s)
    return info


def samloader_flash(cfg: Config, adb: Adb | None, serial: str | None, tars: dict[str, Path], ui,
                    expected_model: str = "") -> bool:
    if not cfg.samloader:
        ui.warn("samloader is not installed - using Odin instead.")
        return False
    info = get_into_download_mode(cfg, adb, serial, ui)
    if not info:
        return False
    if info.model:
        ui.ok(f"Phone in Download mode: {info.model}  (region {info.sales_code or '?'})")
        if expected_model and info.model.upper() != expected_model.upper():
            ui.error(f"Phone reports {info.model} but firmware is for {expected_model}. Stopping - "
                     "flashing the wrong model's firmware can brick a phone.")
            return False
    ui.step("Flashing official firmware. DO NOT unplug the phone. (AP takes several minutes.)")
    code = Samloader(cfg.samloader).flash(tars, ui.info)
    if code != 0:
        ui.warn(f"Automatic flash failed (exit {code}). The phone is safe to re-flash with Odin.")
        return False
    ui.ok("Flash complete. Phone will reboot and wipe itself (first boot can take 5-10 minutes).")
    return True


def odin_flash(cfg: Config, adb: Adb | None, serial: str | None, tars: dict[str, Path], ui) -> bool:
    if adb and serial:
        ui.info("Rebooting phone into Download mode...")
        adb.reboot_download(serial)
    else:
        ui.instruct(["If the phone isn't on the blue 'Downloading...' screen yet:"]
                    + enter_download_mode_instructions())

    if cfg.odin:
        subprocess.Popen([cfg.odin], cwd=str(Path(cfg.odin).parent))
        odin_line = "Odin is opening."
    else:
        odin_line = (f"Open Odin. (Put Odin in {cfg.tools_dir / 'odin'} and this tool will open it for you "
                     "next time - see the README.)")
    if IS_WINDOWS and tars:
        subprocess.Popen(["explorer", str(next(iter(tars.values())).parent)])

    steps = [odin_line, "Check Odin shows a lit-up COM box (that's the phone)."]
    for comp in ("BL", "AP", "CP", "CSC"):
        if comp in tars:
            steps.append(f"Click {comp} and choose: {tars[comp].name}")
    steps += [
        "Do NOT use the HOME_CSC file. Leave 'Re-Partition' unticked. Keep 'Auto Reboot' ticked.",
        "Click Start. AP takes several minutes. Wait for the green PASS box.",
    ]
    ui.instruct(steps)
    return ui.confirm("Did Odin show PASS?")


def flash_phone(cfg: Config, adb: Adb | None, serial: str | None, tars: dict[str, Path], ui,
                expected_model: str = "") -> bool:
    if cfg.settings.get("flash_backend", "samloader") == "samloader" and cfg.samloader:
        try:
            if samloader_flash(cfg, adb, serial, tars, ui, expected_model):
                return True
        except Exception as exc:  # any failure falls back to Odin, which can always recover the phone
            log(f"samloader error: {exc!r}")
            ui.warn(f"samloader error: {exc}")
        if not ui.confirm("Try the guided Odin flash instead?"):
            return False
        adb, serial = None, None  # phone is already in (or headed to) download mode
    return odin_flash(cfg, adb, serial, tars, ui)
