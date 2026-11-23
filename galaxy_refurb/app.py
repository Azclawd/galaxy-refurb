"""Main menu and the per-phone workflow: triage -> flash (wipe) -> final check."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__, final_check, firmware, flash, inventory, triage, ui
from .adb import Adb, AdbError
from .config import IS_WINDOWS, Config, load_config
from .log import current_log, start_log
from .triage import TriageResult

IMEI_CHECK_URL = "https://www.checkmend.com/uk/"

USB_DEBUG_STEPS = [
    "Unlock the phone. If it's in store/Retail mode, exit it first if you can.",
    "Settings > About phone > Software information > tap 'Build number' 7 times.",
    "Settings > Developer options > turn on 'USB debugging'.",
    "Plug the phone into this PC with a data USB cable.",
    "On the phone tick 'Always allow from this computer' and tap Allow.",
]


def open_path(path: Path) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        ui.warn(f"Could not open {path}: {exc}")


# --------------------------------------------------------------------------- triage

def connect_phone(adb: Adb) -> str | None:
    """Wait for an authorised ADB phone. Returns serial, or None for manual mode."""
    ui.instruct(USB_DEBUG_STEPS)
    while True:
        dev = adb.wait_for_device(timeout_s=20)
        if dev and dev[1] == "device":
            return dev[0]
        if dev and dev[1] == "unauthorized":
            ui.warn("Phone found but not allowed yet. Tap 'Allow' on the phone screen.")
            time.sleep(3)
            continue
        choice = ui.ask("No phone found. Press Enter to try again, or type M if this phone can't turn on "
                        "USB debugging (locked in store mode / won't boot)").lower()
        if choice == "m":
            return None


def manual_triage(cfg: Config) -> TriageResult:
    """For phones that can't enable USB debugging: read what we can from Download mode."""
    r = TriageResult()
    ui.info("Manual mode: reading the phone from Download mode instead.")
    info = flash.get_into_download_mode(cfg, None, None, ui) if cfg.samloader else None
    if info:
        r.model, r.serial, r.region = info.model.upper(), info.serial, info.sales_code.upper()
        ui.ok(f"Read from Download mode: {r.model or '?'}  serial {r.serial or '?'}  region {r.region or '?'}")
    else:
        ui.info("Couldn't read it automatically. The model is on the Download-mode screen, the box, or "
                "Settings > About phone.")
    if not r.model:
        r.model = ui.ask("Model number (e.g. SM-S921B)").upper()
    if not r.region:
        r.region = ui.ask("Region/CSC code if known (e.g. BTU, EUX, XEU) - leave blank if unknown").upper()
    r.manual = True
    triage.decide(r)
    return r


def ask_imei(r: TriageResult) -> None:
    ui.info("Get the IMEI: dial *#06# on the phone, or read it from the SIM tray / box label.")
    while True:
        imei = ui.ask("IMEI (15 digits, Enter to skip)").replace(" ", "")
        if not imei:
            return
        if triage.valid_imei(imei):
            r.imei = imei
            return
        ui.warn("That doesn't look like a valid IMEI - check the digits.")


def ask_imei_check(r: TriageResult) -> None:
    if not r.imei:
        return
    if ui.confirm("Check this IMEI against the lost/stolen blacklist now? (opens CheckMEND)"):
        webbrowser.open(IMEI_CHECK_URL)
        while True:
            ans = ui.ask("Result: C = clean, B = blocked/lost/stolen, S = skip").lower()
            if ans in ("c", "b", "s"):
                r.imei_check = {"c": "clean", "b": "blocked", "s": "not checked"}[ans]
                return


def show_triage(r: TriageResult) -> None:
    ui.table("Triage", [
        ("Model", r.model or "?"),
        ("Serial", r.serial or "?"),
        ("Region (CSC)", r.region or "?"),
        ("Firmware", r.firmware or "?"),
        ("Android", r.android or "?"),
        ("IMEI", triage.mask_imei(r.imei) if r.imei else "not entered"),
        ("IMEI check", r.imei_check),
        ("Managed by", "not checked" if r.manual else (r.device_owner or "-")),
        ("Accounts", "not checked" if r.manual else f"{r.google_accounts} Google, {r.samsung_accounts} Samsung"),
        ("Demo firmware", "YES" if r.demo_markers else "no"),
        ("VERDICT", r.verdict),
    ])
    for reason in r.reasons:
        ui.info("- " + reason)


def remove_accounts_loop(adb: Adb, serial: str, r: TriageResult) -> None:
    while r.verdict == triage.REMOVE_ACCOUNTS:
        ui.instruct([
            "On the phone: Settings > Accounts and backup > Manage accounts.",
            "Tap each Google account > Remove account. Do the same for the Samsung account.",
            "(If a password is demanded you don't have, the seller must remove it - press Enter and choose to stop.)",
        ])
        fresh = triage.analyze(triage.collect(adb, serial))
        r.google_accounts, r.samsung_accounts = fresh.google_accounts, fresh.samsung_accounts
        triage.decide(r)
        if r.verdict == triage.REMOVE_ACCOUNTS and not ui.confirm("Accounts still there. Try again?"):
            r.verdict = triage.RETURN
            r.reasons.append("Could not remove signed-in accounts - return to seller.")


def run_triage(cfg: Config, adb: Adb) -> tuple[TriageResult, str | None]:
    ui.step("Step 1 of 3 - Triage")
    start_log(cfg.logs_dir, "phone")
    serial = connect_phone(adb)
    if serial:
        ui.info("Reading phone...")
        r = triage.analyze(triage.collect(adb, serial))
    else:
        r = manual_triage(cfg)
    if not r.serial:
        r.serial = serial or "manual-" + time.strftime("%Y%m%d-%H%M%S")
    if not r.model:
        r.model = ui.ask("Couldn't read the model. Model number (e.g. SM-A156B)").upper()
    if not r.region:
        r.region = ui.ask("Region/CSC code (e.g. BTU for UK, EUX for Europe)").upper()
    ask_imei(r)
    ask_imei_check(r)
    triage.decide(r)
    show_triage(r)
    if serial and r.verdict == triage.REMOVE_ACCOUNTS:
        remove_accounts_loop(adb, serial, r)
        show_triage(r)
    save(cfg, r, triage_verdict=r.verdict, firmware_before=r.firmware, notes=" | ".join(r.reasons))
    return r, serial


def save(cfg: Config, r: TriageResult, **extra: str) -> None:
    log_file = current_log()
    inventory.record(cfg.inventory_csv, serial=r.serial, model=r.model, region=r.region, imei=r.imei,
                     imei_check=r.imei_check, log_file=log_file.name if log_file else "", **extra)


# --------------------------------------------------------------------------- firmware + flash

def get_firmware(cfg: Config, r: TriageResult) -> dict[str, Path] | None:
    ui.step("Step 2 of 3 - Firmware")
    tried_download = False
    while True:
        source = firmware.find_local_firmware(cfg.firmware_dir, r.model, r.region)
        if not source and cfg.samloader and not tried_download:
            tried_download = True
            ui.info(f"Downloading official firmware for {r.model} (region {r.region or '?'}) from Samsung. "
                    "5-12 GB - can take a while. It's saved, so the next phone of this model is instant.")
            source = firmware.download_firmware(cfg.samloader, cfg.settings, cfg.firmware_dir, r.model, r.region,
                                                echo=ui.info)
            if not source:
                ui.warn("Automatic download didn't work (wrong region code for this model, or Samsung's "
                        "server refused). You can add the firmware by hand instead.")
        if source:
            ui.ok(f"Firmware: {source.name}")
            ui.info("Unpacking firmware (first time per model only)...")
            folder = firmware.firmware_folder(source, cfg.work_dir)
            tars = firmware.find_tars(folder)
            missing = [c for c in firmware.COMPONENTS if c not in tars]
            if not missing:
                return tars
            ui.warn(f"Firmware is incomplete - missing {', '.join(missing)}. Expected BL_, AP_, CP_, CSC_ files.")
        target = cfg.firmware_dir / r.model.upper()
        target.mkdir(parents=True, exist_ok=True)
        open_path(target)
        ans = ui.ask(f"Put the firmware zip for {r.model} (region {r.region or 'any'}) into the folder that just "
                     "opened (see README for where to get it). Press Enter to look again, or S to skip this phone")
        if ans.lower() == "s":
            return None


def run_flash(cfg: Config, adb: Adb, serial: str | None, r: TriageResult) -> bool:
    tars = get_firmware(cfg, r)
    if not tars:
        save(cfg, r, final_result="SKIPPED_NO_FIRMWARE")
        return False
    ui.step("Step 2 of 3 - Flash + wipe")
    ui.warn("This ERASES everything on the phone and installs clean official Samsung software.")
    if not ui.confirm("Continue?"):
        return False
    ok = flash.flash_phone(cfg, adb, serial, tars, ui, expected_model=r.model)
    save(cfg, r, final_result="FLASHED" if ok else "FLASH_FAILED")
    if not ok:
        ui.error("Flash did not complete. The phone can be re-flashed - run this phone again. "
                 f"Log: {current_log()}")
    return ok


# --------------------------------------------------------------------------- flows

def process_phone(cfg: Config, adb: Adb) -> None:
    r, serial = run_triage(cfg, adb)
    if r.verdict == triage.RETURN:
        ui.error("This phone should go back to the seller. Recorded in inventory.csv.")
        save(cfg, r, final_result="RETURN_TO_SELLER")
        return
    if r.verdict == triage.REMOVE_ACCOUNTS:
        if not ui.confirm("Accounts are still signed in - flashing now WILL leave it locked. Flash anyway?",
                          default=False):
            return
    if not run_flash(cfg, adb, serial, r):
        return
    run_final(cfg, adb, r)


def run_final(cfg: Config, adb: Adb, r: TriageResult | None = None) -> None:
    if r is None:
        r = TriageResult()
        start_log(cfg.logs_dir, "final-check")
        rows = inventory.load(cfg.inventory_csv)
        serial = ui.ask("Serial number of the phone (Settings > About phone), Enter if unknown")
        row = next((x for x in rows if x.get("serial") == serial), None) if serial else None
        if row:
            r.serial, r.model, r.region, r.imei = row["serial"], row["model"], row["region"], row["imei"]
            r.imei_check = row.get("imei_check", "") or "not checked"
        else:
            r.serial = serial
    ui.step("Step 3 of 3 - Final check")
    result, notes = final_check.run_final_check(adb, r, ui, cfg.settings)
    for n in notes:
        (ui.ok if result == final_check.PASS else ui.info)(n)
    if result == final_check.PASS:
        ui.ok("PASSED - ready to sell.")
    else:
        ui.error(f"Result: {result}")
    save(cfg, r, final_result=result, notes=" | ".join(notes))


def check_tools(cfg: Config) -> Adb | None:
    if not cfg.adb:
        ui.error("adb not found. Re-run the installer (see README).")
        return None
    adb = Adb(cfg.adb)
    try:
        adb.run("start-server", timeout=30)
    except AdbError as exc:
        ui.error(str(exc))
        return None
    if not cfg.samloader:
        ui.warn("samloader not installed - firmware must be added by hand and flashing will use guided Odin.")
    return adb


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        print(__version__)
        return 0
    cfg = load_config()
    ui.step(f"Galaxy Refurb v{__version__}  -  data folder: {cfg.data_home}")
    adb = check_tools(cfg)
    if not adb:
        input("Press Enter to close...")
        return 1
    while True:
        print("\n  1. Process a phone (triage -> flash -> final check)")
        print("  2. Triage only")
        print("  3. Final check only (phone already flashed)")
        print("  4. Open inventory spreadsheet")
        print("  5. Open firmware folder")
        print("  Q. Quit")
        try:
            choice = input("\n  Choose: ").strip().lower()
        except EOFError:
            return 0
        try:
            if choice == "1":
                process_phone(cfg, adb)
            elif choice == "2":
                run_triage(cfg, adb)
            elif choice == "3":
                run_final(cfg, adb)
            elif choice == "4":
                open_path(cfg.inventory_csv) if cfg.inventory_csv.exists() else ui.warn("No phones recorded yet.")
            elif choice == "5":
                open_path(cfg.firmware_dir)
            elif choice in ("q", "quit", "exit"):
                return 0
        except KeyboardInterrupt:
            ui.warn("Cancelled. Back to menu.")
        except EOFError:
            return 0
        except Exception as exc:  # keep the menu alive; details go to the log
            from .log import log
            import traceback
            log(traceback.format_exc())
            ui.error(f"Something went wrong: {exc}")
            ui.info(f"Details saved to {current_log() or cfg.logs_dir}. Send that file for help.")
