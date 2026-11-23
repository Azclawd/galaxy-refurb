"""After the flash: confirm the phone is clean, unlocked and works."""
from __future__ import annotations

import re
import time

from .adb import Adb
from .triage import (GOOGLE_ACCOUNT_TYPE, SAMSUNG_ACCOUNT_TYPE, RawData, TriageResult, collect,
                     count_accounts, find_demo_markers, find_retail_packages, parse_device_owner)

PASS = "PASS_READY_TO_SELL"
FAIL = "FAIL_NEEDS_ATTENTION"
LOCKED_KNOX = "LOCKED_KNOX_RETURN_TO_SELLER"
LOCKED_FRP = "LOCKED_ACCOUNT_RETURN_TO_SELLER"

BATTERY_HEALTH = {"1": "Unknown", "2": "Good", "3": "Overheat", "4": "Dead", "5": "Over voltage",
                  "6": "Failure", "7": "Cold"}


def parse_battery(dump: str) -> tuple[str, str]:
    level = re.search(r"^\s*level:\s*(\d+)", dump, re.MULTILINE)
    health = re.search(r"^\s*health:\s*(\d+)", dump, re.MULTILINE)
    return (level.group(1) if level else "?",
            BATTERY_HEALTH.get(health.group(1), health.group(1)) if health else "?")


def sim_states(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def evaluate(raw: RawData, before: TriageResult) -> tuple[list[str], list[str]]:
    """Return (problems, notes) for a freshly flashed phone."""
    problems: list[str] = []
    notes: list[str] = []
    p = raw.props

    owner, profile = parse_device_owner(raw.device_policy)
    if owner:
        problems.append(f"Still managed by '{owner}'. Phone is enrolled - return to seller to release it.")
    if profile:
        problems.append(f"Work profile managed by '{profile}' is present.")
    retail = find_retail_packages(raw.packages)
    if retail:
        problems.append(f"Store/demo apps still installed: {', '.join(retail)}.")
    demo = find_demo_markers(p)
    if demo:
        problems.append("Still on demo (LDU) firmware: " + "; ".join(demo))
    g = count_accounts(raw.accounts, GOOGLE_ACCOUNT_TYPE)
    s = count_accounts(raw.accounts, SAMSUNG_ACCOUNT_TYPE)
    if g or s:
        problems.append(f"Accounts signed in ({g} Google, {s} Samsung). Remove them before the final reset.")
    model = p.get("ro.product.model", "").strip()
    if before.model and model and model != before.model:
        problems.append(f"Model changed from {before.model} to {model} - wrong firmware?")
    warranty = p.get("ro.boot.warranty_bit", "")
    if warranty and warranty not in ("0", "0x0"):
        notes.append("Knox warranty bit tripped - disclose in listing.")
    sims = sim_states(p.get("gsm.sim.state", ""))
    if "READY" in sims or "LOADED" in sims:
        notes.append("SIM detected and ready.")
    elif sims and all(x == "ABSENT" for x in sims):
        notes.append("No SIM inserted - SIM/network not tested automatically.")
    elif sims:
        notes.append(f"SIM state: {','.join(sims)}")
    notes.append(f"Firmware now: {p.get('ro.build.PDA', '?')}  Android {p.get('ro.build.version.release', '?')}  "
                 f"Region {p.get('ro.csc.sales_code', '?')}")
    return problems, notes


def wait_for_authorized(adb: Adb, ui, minutes: float) -> str | None:
    """Wait until a phone shows up as 'device' in adb. Returns its serial."""
    end = time.time() + minutes * 60
    told_unauth = False
    while time.time() < end:
        try:
            devs = adb.devices()
        except Exception:
            devs = []
        for serial, state in devs:
            if state == "device":
                return serial
            if state == "unauthorized" and not told_unauth:
                ui.instruct(["On the phone, tick 'Always allow from this computer' and tap Allow."])
                told_unauth = True
        time.sleep(2)
    return None


def run_final_check(adb: Adb, before: TriageResult, ui, settings: dict) -> tuple[str, list[str]]:
    ui.step("Final check")
    ui.instruct([
        "Wait for the phone to finish booting (the first boot after a flash can take 5-10 minutes).",
        "Go through the setup screens. Connect to Wi-Fi. SKIP every Google and Samsung account sign-in.",
        "Watch for a screen saying the phone 'belongs to an organisation', 'is managed', Knox, or a store name.",
        "Watch for a screen asking you to verify a PREVIOUS Google or Samsung account.",
    ])
    if ui.confirm("Did you see an organisation / managed device / Knox enrollment screen?", default=False):
        return LOCKED_KNOX, ["Knox enrollment after flash - the seller must release this IMEI from their Knox account."]
    if ui.confirm("Did it ask you to verify a previous Google or Samsung account?", default=False):
        return LOCKED_FRP, ["Account lock (FRP / Reactivation lock) - the previous owner must remove the account."]

    ui.instruct([
        "Finish setup to the home screen.",
        "Settings > About phone > Software information > tap 'Build number' 7 times (enter PIN if asked).",
        "Settings > Developer options > turn on 'USB debugging'.",
        "Plug in the USB cable (if unplugged) and tap Allow on the phone.",
    ])
    serial = wait_for_authorized(adb, ui, settings.get("first_boot_wait_minutes", 15))
    if not serial:
        return FAIL, ["Could not connect over USB after the flash - check USB debugging and run the final check again."]

    raw = collect(adb, serial)
    problems, notes = evaluate(raw, before)
    level, health = parse_battery(adb.shell(serial, "dumpsys battery"))
    notes.append(f"Battery {level}% - health: {health}")
    if health not in ("Good", "?"):
        problems.append(f"Battery health reported as {health}.")

    imei_hint = f" (ends {before.imei[-4:]})" if before.imei else ""
    ui.instruct(["On the phone open the Phone app and dial *#06#"])
    if not ui.confirm(f"Does it show a proper 15-digit IMEI{imei_hint} (not blank, 0 or 'null')?"):
        problems.append("IMEI missing/invalid after flash - typical of demo units. Cannot be sold as a working phone.")
    if ui.confirm("Do you have a SIM to test calling? (insert it now if yes)", default=False):
        ui.instruct(["Make a short test call and send a text."])
        if ui.confirm("Did the call and text work?"):
            notes.append("Test call + SMS OK.")
        else:
            problems.append("Calling/SMS failed with a SIM inserted.")
    if ui.confirm("Quick hardware check - screen, touch, cameras, speakers, mic, buttons, charging all OK?"):
        notes.append("Manual hardware check OK.")
    else:
        problems.append("Hardware fault reported during check - note it in the listing.")

    if problems:
        return FAIL, problems + notes

    ui.instruct([
        "Last step - make it fresh for the buyer:",
        "Settings > General management > Reset > Factory data reset > Reset.",
        "The phone will wipe and show the 'Welcome' screen. It's ready to box.",
    ])
    return PASS, notes
