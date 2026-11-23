"""The one-button process: every step reports a status the page shows as a checklist.

Where a person has to do something (tap Allow, finish setup), the step shows a card and,
wherever possible, moves on by itself as soon as it sees the phone has done it.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import diagnose, final_check, firmware, flash, inventory, triage
from .adb import Adb, AdbError
from .config import Config
from .diagnose import Diagnosis
from .log import current_log, start_log
from .proc import run_stream
from .session import Cancelled, Session
from .triage import TriageResult

STEPS = [
    ("connect", "Connect phone"),
    ("read", "Read phone details"),
    ("imei", "IMEI & blacklist check"),
    ("locks", "Accounts & store locks"),
    ("firmware", "Get official firmware"),
    ("flash", "Install firmware & wipe"),
    ("setup", "First start-up"),
    ("reconnect", "Reconnect phone"),
    ("verify", "Final checks"),
    ("reset", "Fresh reset for the buyer"),
]

VERIFY_STEPS = [
    ("connect", "Connect phone"),
    ("read", "Read phone details"),
    ("verify", "Final checks"),
    ("reset", "Fresh reset for the buyer"),
]

MIN_FREE_GB = 25  # zip + unpacked copy of the biggest firmware

IMEI_CHECK_URL = "https://www.checkmend.com/uk/"

USB_DEBUG_STEPS = [
    "Unlock the phone (exit store / Retail mode first if it's in it).",
    "Open Settings > About phone > Software information.",
    "Tap 'Build number' 7 times (enter the PIN if asked).",
    "Go back to Settings > Developer options and turn on 'USB debugging'.",
    "Tick 'Always allow from this computer' and tap Allow.",
]


class Stop(Exception):
    """Ends the job with a result card."""

    def __init__(self, status: str, title: str, diagnosis: Diagnosis | None = None, detail: str = ""):
        super().__init__(title)
        self.status, self.title, self.diagnosis, self.detail = status, title, diagnosis, detail


def fmt_clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Ticker:
    """Keeps a running step's detail line alive: '<text> · 03:12', refreshed every second.

    `extra()` may return live numbers (e.g. bytes downloaded) to append.
    """

    def __init__(self, session: Session, key: str, text: str, extra=None):
        self.s, self.key, self.text, self.extra = session, key, text, extra
        self.t0 = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(2)

    def set(self, text: str) -> None:
        self.text = text
        self._push()

    def _push(self) -> None:
        parts = [self.text]
        if self.extra:
            try:
                e = self.extra()
            except Exception:
                e = ""
            if e:
                parts.append(e)
        parts.append(fmt_clock(time.time() - self.t0))
        self.s.set_step(self.key, None, " · ".join(parts))

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            self._push()


def parse_service_call_imei(text: str) -> str:
    """Extract an IMEI from `service call iphonesubinfo` parcel output ('3.5.8.…' text columns)."""
    chunks = re.findall(r"'([^']*)'", text)
    digits = re.sub(r"\D", "", "".join(chunks))
    m = re.search(r"\d{15}", digits)
    return m.group(0) if m else ""


class Workflow:
    def __init__(self, cfg: Config, session: Session, adb: Adb | None, mode: str = "full"):
        self.cfg, self.s, self.adb, self.mode = cfg, session, adb, mode
        self.r = TriageResult()
        self.serial: str | None = None
        self.notes: list[str] = []

    # ------------------------------------------------------------------ helpers
    def log(self, msg: str, level: str = "info") -> None:
        self.s.add_log(msg, level)

    def step(self, key: str, status: str, detail: str | None = None) -> None:
        self.s.set_step(key, status, detail)

    def save(self, **extra: str) -> None:
        lf = current_log()
        inventory.record(self.cfg.inventory_csv, serial=self.r.serial, model=self.r.model, region=self.r.region,
                         imei=self.r.imei, imei_check=self.r.imei_check, firmware_before=self.r.firmware,
                         log_file=lf.name if lf else "", **extra)

    def wait_until(self, check, title: str, *, text: str = "", steps: list[str] | None = None,
                   options: list[dict] | None = None, waiting: str = "Waiting for the phone…",
                   interval: float = 2.0):
        """Show a card and poll `check()` until it returns something truthy or the user clicks an option.

        Returns ("auto", value) or ("answer", option_value).
        """
        pid = self.s.show_prompt("steps", title, text=text, steps=steps, options=options or [], waiting=waiting)
        while True:
            value = check()
            if value:
                self.s.close_prompt(pid)
                return "auto", value
            answered, ans = self.s.poll_answer(pid, interval)
            if answered:
                return "answer", ans

    def adb_devices(self) -> list[tuple[str, str]]:
        if not self.adb:
            return []
        try:
            return self.adb.devices()
        except AdbError:
            return []

    def adb_ready(self) -> str | None:
        for serial, state in self.adb_devices():
            if state == "device":
                return serial
        return None

    def download_mode(self) -> flash.DownloadModeInfo | None:
        return flash.Samloader(self.cfg.samloader).detect() if self.cfg.samloader else None

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        steps = VERIFY_STEPS if self.mode == "verify" else STEPS
        self.s.new_job(steps, mode=self.mode)
        start_log(self.cfg.logs_dir, "phone")
        current = "connect"
        fns = {"connect": self.connect, "read": self.read, "imei": self.imei, "locks": self.locks,
               "firmware": self.get_firmware, "flash": self.do_flash, "setup": self.setup,
               "reconnect": self.reconnect, "verify": self.verify, "reset": self.reset}
        try:
            for key, _title in steps:
                fn = fns[key]
                current = key
                self.s.check_cancel()
                self.step(key, "running")
                fn()
                if self.s._step(key)["status"] == "running":
                    self.step(key, "done")
            self.save(final_result=final_check.PASS, notes=" | ".join(["All checks passed", *self.notes]))
            self.s.finish("pass", "Ready to sell", "All checks passed. Box it up.")
        except Stop as e:
            self.step(current, "failed" if e.status != "return" else "blocked")
            result = {"return": "RETURN_TO_SELLER", "fail": final_check.FAIL}.get(e.status, e.status.upper())
            self.save(final_result=result, notes=e.title + (" | " + " | ".join(self.notes) if self.notes else ""))
            self.s.finish(e.status, e.title, e.detail, e.diagnosis)
        except Cancelled:
            self.step(current, "skipped", "Stopped")
            self.save(final_result="STOPPED")
            self.s.finish("stopped", "Stopped", "You stopped this phone. Press 'Next phone' to start again.")
        except Exception as exc:  # unexpected: show it, never crash the app
            tb = traceback.format_exc()
            self.log(tb, "error")
            self.step(current, "failed")
            self.save(final_result="ERROR", notes=str(exc))
            self.s.finish("fail", "Something unexpected went wrong", str(exc),
                          Diagnosis("UNEXPECTED", "Unexpected error", str(exc),
                                    ["Run the phone again.", f"If it repeats, send the log file: {current_log()}"],
                                    tb.strip().splitlines()[-12:]))

    # ------------------------------------------------------------------ 1 connect
    def connect(self) -> None:
        serial = self.adb_ready()
        if serial:
            self.serial = serial
            self.step("connect", "done", "Connected over USB")
            return
        if self.mode != "verify" and self.download_mode():
            self.r.manual = True
            self.step("connect", "done", "Phone is in Download mode")
            return
        unauthorized = any(st == "unauthorized" for _, st in self.adb_devices())
        how, val = self.wait_until(
            self.adb_ready,
            "Tap 'Allow' on the phone" if unauthorized else "Turn on USB debugging",
            text=("A message on the phone asks to allow USB debugging." if unauthorized else
                  "This lets the PC read the phone. One-time per phone - takes about 30 seconds."),
            steps=(["Tick 'Always allow from this computer'.", "Tap Allow."] if unauthorized else USB_DEBUG_STEPS),
            options=([] if self.mode == "verify" else
                     [{"value": "download", "label": "Phone won't allow it - use Download mode", "style": "ghost"}]),
            waiting="Watching for the phone… this moves on by itself.")
        if how == "auto":
            self.serial = val
            self.step("connect", "done", "Connected over USB")
            return
        # Download-mode route for locked-down store phones.
        how, info = self.wait_until(
            self.download_mode, "Put the phone in Download mode",
            steps=flash.enter_download_mode_instructions(), waiting="Watching for Download mode…")
        self.r.manual = True
        self.step("connect", "done", "Phone is in Download mode")

    # ------------------------------------------------------------------ 2 read
    def read(self) -> None:
        if self.serial and self.adb:
            self.r = triage.analyze(triage.collect(self.adb, self.serial))
        else:
            info = self.download_mode() or flash.DownloadModeInfo()
            self.r.model, self.r.serial, self.r.region = info.model.upper(), info.serial, info.sales_code.upper()
            self.r.manual = True
        if not self.r.model:
            self.r.model = str(self.s.ask("text", "What's the model number?",
                                          text="It's on the Download-mode screen, the box, or Settings > About phone.",
                                          placeholder="e.g. SM-S921B")).strip().upper()
        if not self.r.serial:
            self.r.serial = self.serial or "manual-" + time.strftime("%Y%m%d-%H%M%S")
        start_log(self.cfg.logs_dir, self.r.serial)
        if self.mode == "verify":
            row = next((x for x in inventory.load(self.cfg.inventory_csv) if x.get("serial") == self.r.serial), None)
            if row:
                self.r.imei, self.r.imei_check = row.get("imei", ""), row.get("imei_check", "") or "not checked"
                self.r.model = self.r.model or row.get("model", "")
        self.s.set_phone(model=self.r.model, serial=self.r.serial, region=self.r.region or "?",
                         firmware=self.r.firmware, android=self.r.android)
        self.step("read", "done", f"{self.r.model} · region {self.r.region or '?'}")
        self.log(f"Phone: {self.r.model} serial {self.r.serial} region {self.r.region} firmware {self.r.firmware}")

    # ------------------------------------------------------------------ 3 imei
    def imei(self) -> None:
        if self.serial and self.adb:
            for cmd in ("service call iphonesubinfo 1 s16 com.android.shell", "service call iphonesubinfo 1"):
                found = parse_service_call_imei(self.adb.shell(self.serial, cmd))
                if triage.valid_imei(found):
                    self.r.imei = found
                    self.s.add_check("imei", "IMEI read automatically", "done", triage.mask_imei(found))
                    break
        error = ""
        while not self.r.imei:
            val = self.s.ask("text", "Enter the IMEI", text="Dial *#06# on the phone, or read it from the box / SIM tray.",
                             placeholder="15 digits", error=error,
                             options=[{"value": "__skip__", "label": "Skip", "style": "ghost"}])
            val = str(val).replace(" ", "")
            if val == "__skip__":
                self.s.add_check("imei", "IMEI not entered", "warn")
                break
            if triage.valid_imei(val):
                self.r.imei = val
                self.s.add_check("imei", "IMEI entered", "done", triage.mask_imei(val))
            else:
                error = "That isn't a valid IMEI - check the 15 digits."
        if self.r.imei:
            self.s.set_phone(imei=triage.mask_imei(self.r.imei))
            ans = self.s.ask("choice", "Is the IMEI clean?",
                             text=f"Check IMEI {self.r.imei} on CheckMEND (opens in your browser), then choose.",
                             link={"url": IMEI_CHECK_URL, "label": "Open CheckMEND"},
                             options=[{"value": "clean", "label": "Clean", "style": "good"},
                                      {"value": "blocked", "label": "Blacklisted", "style": "bad"},
                                      {"value": "skip", "label": "Check later", "style": "ghost"}])
            self.r.imei_check = {"clean": "clean", "blocked": "blocked"}.get(str(ans), "not checked")
            if ans == "blocked":
                self.s.add_check("imei", "Blacklist check", "failed", "Blacklisted")
                raise Stop("return", "Return to seller: IMEI blacklisted", diagnose.known("IMEI_BLOCKED"))
            self.s.add_check("imei", "Blacklist check", "done" if ans == "clean" else "warn",
                             "Clean" if ans == "clean" else "Not checked yet")
        status = "done" if self.r.imei_check == "clean" else "warn"
        self.step("imei", status)

    # ------------------------------------------------------------------ 4 locks
    def locks(self) -> None:
        r = self.r
        if r.manual:
            self.s.add_check("locks", "Accounts & management", "warn", "Can't check in Download mode - final check will")
            self.step("locks", "warn")
            return
        self.s.add_check("locks", "Store management", "warn" if r.device_owner else "done",
                         f"Managed by {r.device_owner} - removed by the wipe" if r.device_owner else "Not managed")
        self.s.add_check("locks", "Store / demo apps", "warn" if r.retail_packages else "done",
                         f"{len(r.retail_packages)} found - removed by the wipe" if r.retail_packages else "None")
        self.s.add_check("locks", "Demo firmware", "warn" if r.demo_markers else "done",
                         "Demo (LDU) - will be replaced" if r.demo_markers else "Retail")
        if r.warranty_bit and r.warranty_bit not in ("0", "0x0"):
            self.s.add_check("locks", "Knox warranty bit", "warn", "Tripped - mention in listing")
            self.notes.append("Knox warranty bit tripped")

        def accounts_gone():
            fresh = triage.analyze(triage.collect(self.adb, self.serial))  # type: ignore[arg-type]
            r.google_accounts, r.samsung_accounts = fresh.google_accounts, fresh.samsung_accounts
            return (r.google_accounts + r.samsung_accounts) == 0

        if r.google_accounts or r.samsung_accounts:
            how, ans = self.wait_until(
                accounts_gone, "Remove the signed-in accounts",
                text=f"{r.google_accounts} Google and {r.samsung_accounts} Samsung account(s) are signed in. "
                     "If they stay, the phone locks after the wipe.",
                steps=["Settings > Accounts and backup > Manage accounts.",
                       "Tap each account > Remove account.", "Do the same for the Samsung account."],
                options=[{"value": "cant", "label": "It asks for a password I don't have", "style": "bad"}],
                waiting="Checking every few seconds… moves on when they're gone.", interval=4.0)
            if how == "answer":
                self.s.add_check("locks", "Signed-in accounts", "failed", "Couldn't remove")
                raise Stop("return", "Return to seller: account can't be removed", diagnose.known("ACCOUNTS_LOCKED"))
        self.s.add_check("locks", "Signed-in accounts", "done", "None")
        self.step("locks", "done")

    # ------------------------------------------------------------------ 5 firmware
    def get_firmware(self) -> dict[str, Path]:
        r = self.r
        while True:
            source = firmware.find_local_firmware(self.cfg.firmware_dir, r.model, r.region)
            if not source:
                source = self.download()
            if source:
                prog = {"pct": 0}
                with Ticker(self.s, "firmware", "Unpacking firmware (first time per model only)",
                            extra=lambda: f"{prog['pct']}%" if prog["pct"] else "") as tk:
                    folder = firmware.firmware_folder(
                        source, self.cfg.work_dir, progress=lambda d, t: prog.__setitem__("pct", int(d * 100 / t)))
                tars = firmware.find_tars(folder)
                missing = [c for c in firmware.COMPONENTS if c not in tars]
                if not missing:
                    self.tars = tars
                    ap = re.sub(r"\.tar(\.md5)?$", "", tars["AP"].name, flags=re.I)
                    ver = ap.split("_")[1] if "_" in ap else source.name
                    self.step("firmware", "done", f"Official firmware {ver}")
                    return tars
                self.log(f"Firmware incomplete, missing {missing}", "error")
            self.firmware_problem(source)

    def firmware_problem(self, source) -> None:
        target = self.cfg.firmware_dir / self.r.model.upper()
        target.mkdir(parents=True, exist_ok=True)
        diag = self.last_diag if getattr(self, "last_diag", None) else diagnose.known("FIRMWARE_NOT_FOUND")
        self.step("firmware", "failed", diag.title)
        ans = self.s.ask("choice", diag.title, diagnosis=diag,
                         options=[{"value": "retry", "label": "Retry", "style": "primary"},
                                  {"value": "region", "label": "Try another region", "style": "ghost"},
                                  {"value": "folder", "label": "I'll add the firmware myself", "style": "ghost"},
                                  {"value": "stop", "label": "Stop", "style": "bad"}])
        self.last_diag = None
        if ans == "stop":
            raise Stop("fail", "No firmware for this phone", diag)
        if ans == "region":
            self.r.region = str(self.s.ask("text", "Region code to try", placeholder="e.g. EUX, EUY, XEU, XAA")).strip().upper()
        if ans == "folder":
            from .app import open_path
            open_path(target)
            self.wait_until(lambda: firmware.find_local_firmware(self.cfg.firmware_dir, self.r.model, self.r.region),
                            "Add the firmware",
                            text=f"Put the firmware zip (or BL/AP/CP/CSC files) in the folder that just opened: {target}",
                            waiting="Watching the folder…")
        self.step("firmware", "running", "")

    def download(self) -> Path | None:
        r = self.r
        if not self.cfg.samloader:
            self.last_diag = diagnose.known("FIRMWARE_NOT_FOUND")
            return None
        self.step("firmware", "running", "Asking Samsung for the latest firmware…")
        chosen = None
        for region in firmware.regions_to_try(r.region):
            version = firmware.check_update(self.cfg.samloader, r.model, region)
            if version:
                chosen, self.fw_version = region, version
                self.log(f"Samsung has {version} for {r.model} / {region}")
                break
            self.log(f"No firmware for {r.model} / {region}")
        if not chosen:
            self.last_diag = diagnose.known("FIRMWARE_NOT_FOUND")
            return None
        version = self.fw_version
        out_dir = self.cfg.firmware_dir / r.model.upper() / chosen
        out_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(out_dir).free / 1024 ** 3
        if free_gb < MIN_FREE_GB:
            self.last_diag = diagnose.Diagnosis(
                "DISK_FULL", "Not enough disk space",
                f"Only {free_gb:.0f} GB free. Firmware needs about {MIN_FREE_GB} GB (download + unpacked copy).",
                ["Free up space, or delete old model folders in Documents\\GalaxyRefurb\\firmware "
                 "(Firmware folder button, top right).", "Then press Retry."])
            return None
        self.s.add_check("firmware", f"Found on Samsung's servers ({chosen})", "done", version)
        start_bytes = firmware.folder_bytes(out_dir)
        rate = {"t": time.time(), "b": start_bytes, "speed": 0.0}

        def progress() -> str:
            now_b = firmware.folder_bytes(out_dir)
            dt = time.time() - rate["t"]
            if dt >= 2:
                rate["speed"] = max(0.0, (now_b - rate["b"]) / dt)
                rate["t"], rate["b"] = time.time(), now_b
            got = firmware.human_size(max(0, now_b - start_bytes))
            return f"{got}" + (f" at {firmware.human_size(rate['speed'])}/s" if rate["speed"] else "")

        cmd = firmware.download_command(self.cfg.samloader, r.model, chosen, out_dir,
                                        int(self.cfg.settings.get("download_threads", 8)))
        with Ticker(self.s, "firmware", f"Downloading {r.model} firmware (5-12 GB, first time only)",
                    extra=progress):
            code, tail = run_stream(cmd, None, timeout=8 * 3600)
        if code != 0:
            self.last_diag = diagnose.diagnose_download(tail)
            return None
        zips = sorted(out_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        return zips[0] if zips else None

    # ------------------------------------------------------------------ 6 flash
    def do_flash(self) -> None:
        tars = self.tars
        ans = self.s.ask("choice", "Erase and install clean software?",
                         text="This wipes everything on the phone and installs official Samsung software. "
                              "Don't unplug the phone until it's done.",
                         options=[{"value": "go", "label": "Erase & install", "style": "primary"},
                                  {"value": "stop", "label": "Not now", "style": "ghost"}])
        if ans != "go":
            raise Cancelled()
        use_odin = self.cfg.settings.get("flash_backend") == "odin" or not self.cfg.samloader
        while True:
            if use_odin:
                if self.odin_flash(tars):
                    break
                raise Stop("fail", "Flash not completed", Diagnosis("ODIN_FAILED", "Odin didn't pass",
                           "Odin reported a failure or the flash was abandoned.",
                           ["Check the cable and driver, then run the phone again."]))
            ok, diag = self.auto_flash(tars)
            if ok:
                break
            self.step("flash", "failed", diag.title)
            ans = self.s.ask("choice", diag.title, diagnosis=diag,
                             options=[{"value": "retry", "label": "Retry", "style": "primary"},
                                      {"value": "odin", "label": "Use Odin instead", "style": "ghost"},
                                      {"value": "stop", "label": "Stop", "style": "bad"}])
            if ans == "stop":
                raise Stop("fail", diag.title, diag)
            use_odin = ans == "odin"
            self.step("flash", "running", "")
        self.step("flash", "done", "Clean firmware installed, phone wiped")

    def enter_download_mode(self) -> flash.DownloadModeInfo:
        info = self.download_mode()
        if info:
            return info
        if self.serial and self.adb:
            self.step("flash", "running", "Restarting phone into Download mode…")
            self.adb.reboot_download(self.serial)
            end = time.time() + 40
            while time.time() < end:
                info = self.download_mode()
                if info:
                    return info
                time.sleep(2)
        how, info = self.wait_until(self.download_mode, "Put the phone in Download mode",
                                    steps=flash.enter_download_mode_instructions(),
                                    waiting="Watching for Download mode…")
        return info

    def auto_flash(self, tars: dict[str, Path]) -> tuple[bool, Diagnosis]:
        info = self.enter_download_mode()
        self.s.add_check("flash", "Download mode", "done", info.model or "Connected")
        if info.model and self.r.model and info.model.upper() != self.r.model.upper():
            raise Stop("fail", "Firmware doesn't match this phone", diagnose.known(
                "WRONG_MODEL", [f"Phone reports {info.model}, firmware is for {self.r.model}"]))
        with Ticker(self.s, "flash", "Installing - don't unplug (usually 5-10 min)") as tk:
            def on_line(text: str) -> None:
                m = re.search(r"(\d{1,3})\s*%", text)
                if m:
                    tk.set(f"Installing {m.group(1)}% - don't unplug")
                elif re.search(r"upload|flash|writ|send|partition|erase|reboot", text, re.I):
                    tk.set(f"{text[:60]} - don't unplug")

            code, tail = run_stream(flash.flash_command(self.cfg.samloader, tars), on_line)  # type: ignore[arg-type]
        if code == 0:
            return True, None  # type: ignore[return-value]
        return False, diagnose.diagnose_flash(tail)

    def odin_flash(self, tars: dict[str, Path]) -> bool:
        import subprocess
        from .config import IS_WINDOWS
        if self.cfg.odin:
            subprocess.Popen([self.cfg.odin], cwd=str(Path(self.cfg.odin).parent))
        if IS_WINDOWS:
            subprocess.Popen(["explorer", str(next(iter(tars.values())).parent)])
        steps = ["Odin is opening." if self.cfg.odin else
                 f"Open Odin (put it in {self.cfg.tools_dir / 'odin'} to have it open automatically).",
                 "Make sure the phone is on the blue 'Downloading...' screen."]
        steps += [f"Click {c} and choose {tars[c].name}" for c in ("BL", "AP", "CP", "CSC") if c in tars]
        steps += ["Do NOT use HOME_CSC. Leave Re-Partition unticked.", "Click Start and wait for the green PASS."]
        ans = self.s.ask("choice", "Flash with Odin", steps=steps,
                         options=[{"value": "pass", "label": "Odin showed PASS", "style": "good"},
                                  {"value": "fail", "label": "It failed", "style": "bad"}])
        return ans == "pass"

    # ------------------------------------------------------------------ 7 setup
    def setup(self) -> None:
        ans = self.s.ask("choice", "Set up the phone",
                         text="The first start-up takes 5-10 minutes. Then go through the setup screens.",
                         steps=["Wait for the Welcome screen.", "Connect to Wi-Fi.",
                                "SKIP every Google and Samsung sign-in.", "Choose what you saw:"],
                         options=[{"value": "ok", "label": "Normal setup, reached home screen", "style": "good"},
                                  {"value": "knox", "label": "'Belongs to an organisation' / Knox / managed", "style": "bad"},
                                  {"value": "frp", "label": "Asked for a previous Google/Samsung account", "style": "bad"},
                                  {"value": "stuck", "label": "Stuck on logo / keeps restarting", "style": "bad"}])
        if ans == "knox":
            raise Stop("return", "Return to seller: Knox locked", diagnose.known("KNOX_ENROLLED"))
        if ans == "frp":
            raise Stop("return", "Return to seller: activation lock", diagnose.known("FRP_LOCKED"))
        if ans == "stuck":
            raise Stop("fail", "Phone didn't start", diagnose.known("BOOTLOOP"))
        self.step("setup", "done", "No locks at setup")

    # ------------------------------------------------------------------ 8 reconnect
    def reconnect(self) -> None:
        how, val = self.wait_until(
            self.adb_ready, "Turn USB debugging back on",
            text="The wipe turned it off. Same steps as before:", steps=USB_DEBUG_STEPS,
            options=[{"value": "cant", "label": "I can't get it to connect", "style": "bad"}],
            waiting="Watching for the phone… moves on by itself.")
        if how == "answer":
            raise Stop("fail", "Couldn't reconnect", diagnose.known("POST_FLASH_NO_USB"))
        self.serial = val
        self.step("reconnect", "done", "Connected")

    # ------------------------------------------------------------------ 9 verify
    def verify(self) -> None:
        assert self.adb and self.serial
        raw = triage.collect(self.adb, self.serial)
        p = raw.props
        problems: list[str] = []

        def check(title: str, ok: bool, good: str, bad: str, warn_only: bool = False) -> None:
            self.s.add_check("verify", title, "done" if ok else ("warn" if warn_only else "failed"), good if ok else bad)
            if not ok and not warn_only:
                problems.append(f"{title}: {bad}")
            time.sleep(0.35)  # let each tick land visibly

        owner, profile = triage.parse_device_owner(raw.device_policy)
        check("Not managed by store", not owner and not profile, "Clean", f"Still managed by {owner or profile}")
        retail = triage.find_retail_packages(raw.packages)
        check("No store / demo apps", not retail, "None", ", ".join(retail))
        demo = triage.find_demo_markers(p)
        check("Retail firmware", not demo, p.get("ro.build.PDA", "OK"), "Still demo firmware")
        g = triage.count_accounts(raw.accounts, triage.GOOGLE_ACCOUNT_TYPE)
        sa = triage.count_accounts(raw.accounts, triage.SAMSUNG_ACCOUNT_TYPE)
        check("No accounts signed in", g + sa == 0, "None", f"{g} Google, {sa} Samsung")
        model = p.get("ro.product.model", "").strip()
        check("Model matches", not self.r.model or not model or model == self.r.model, model or "OK",
              f"{model} ≠ {self.r.model}")
        level, health = final_check.parse_battery(self.adb.shell(self.serial, "dumpsys battery"))
        check("Battery", health in ("Good", "?"), f"{health} · {level}%", f"{health} · {level}%")
        self.s.set_phone(firmware=p.get("ro.build.PDA", ""), android=p.get("ro.build.version.release", ""),
                         battery=f"{health} · {level}%")

        imei_now = parse_service_call_imei(self.adb.shell(self.serial, "service call iphonesubinfo 1 s16 com.android.shell"))
        if triage.valid_imei(imei_now):
            check("IMEI present", not self.r.imei or imei_now == self.r.imei, triage.mask_imei(imei_now),
                  "IMEI changed")
        else:
            hint = f" ending {self.r.imei[-4:]}" if self.r.imei else ""
            ans = self.s.ask("choice", "Check the IMEI", text=f"Dial *#06# on the phone. Does it show a 15-digit IMEI{hint}?",
                             options=[{"value": "yes", "label": "Yes", "style": "good"},
                                      {"value": "no", "label": "No / blank / 0", "style": "bad"}])
            check("IMEI present", ans == "yes", "Shown on *#06#", "Missing - typical of demo units")

        sims = final_check.sim_states(p.get("gsm.sim.state", ""))
        ans = self.s.ask("choice", "Test a SIM (optional)",
                         text=("SIM detected. " if "READY" in sims or "LOADED" in sims else "")
                              + "Put a SIM in, make a short call and send a text.",
                         options=[{"value": "ok", "label": "Call & text worked", "style": "good"},
                                  {"value": "fail", "label": "They failed", "style": "bad"},
                                  {"value": "skip", "label": "No SIM to test", "style": "ghost"}])
        if ans == "skip":
            self.s.add_check("verify", "Calls & texts", "warn", "Not tested")
            self.notes.append("SIM not tested")
        else:
            check("Calls & texts", ans == "ok", "Working", "Failed with SIM inserted")

        ans = self.s.ask("choice", "Quick hardware check",
                         steps=["Screen & touch", "Front + back cameras", "Speaker, earpiece, microphone",
                                "Buttons", "Charging"],
                         options=[{"value": "ok", "label": "All fine", "style": "good"},
                                  {"value": "fault", "label": "Found a fault", "style": "bad"}])
        if ans == "fault":
            what = self.s.ask("text", "What's the fault?", placeholder="e.g. cracked back glass, weak speaker")
            check("Hardware", False, "", str(what) or "Fault reported")
        else:
            check("Hardware", True, "All fine", "")

        if problems:
            raise Stop("fail", "Needs attention", diagnose.diagnose_verify(problems))
        self.step("verify", "done", "Everything checks out")

    # ------------------------------------------------------------------ 10 reset
    def reset(self) -> None:
        self.s.ask("steps", "Last step: fresh reset for the buyer",
                   steps=["Settings > General management > Reset.", "Factory data reset > Reset.",
                          "Wait for the Welcome screen, then unplug."],
                   options=[{"value": "done", "label": "Done", "style": "primary"}])
        self.step("reset", "done", "Welcome screen")
