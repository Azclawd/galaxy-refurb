"""Turn failures into plain-English diagnoses: what happened, why, and what to do."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Diagnosis:
    code: str
    title: str
    cause: str
    fix: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    severity: str = "error"   # "error" (needs attention) or "return" (send back to seller)

    def to_dict(self) -> dict:
        return asdict(self)


DRIVER_FIX = "Install the Samsung USB driver (Help > Samsung driver), then unplug and replug the phone."
CABLE_FIX = "Use a USB data cable (not a charge-only cable) plugged straight into the PC, not a hub."

KNOWN: dict[str, Diagnosis] = {
    "IMEI_BLOCKED": Diagnosis(
        "IMEI_BLOCKED", "IMEI is blacklisted",
        "This phone's IMEI is reported lost, stolen or unpaid. It can't be legally resold as a working phone.",
        ["Set it aside and return it to the seller with the IMEI as evidence."], severity="return"),
    "ACCOUNTS_LOCKED": Diagnosis(
        "ACCOUNTS_LOCKED", "Accounts couldn't be removed",
        "A Google or Samsung account is still signed in. After a wipe the phone would demand that "
        "account's password (activation lock).",
        ["Ask the seller to remove the account or give the password.",
         "Don't flash it until the account is gone - it would lock."], severity="return"),
    "KNOX_ENROLLED": Diagnosis(
        "KNOX_ENROLLED", "Locked to the store's Knox account",
        "After the wipe the phone contacted Samsung and re-enrolled into the retailer's Knox management. "
        "Its IMEI is registered to them. No software can legitimately remove this.",
        ["Send the seller the IMEI and ask them to release it from Knox Mobile Enrollment / Knox Manage.",
         "Once released, run the phone through again."], severity="return"),
    "FRP_LOCKED": Diagnosis(
        "FRP_LOCKED", "Activation lock (previous account)",
        "The phone asks for the previous Google/Samsung account. That account wasn't removed before the wipe.",
        ["Only the account owner can remove it - return to the seller with the IMEI."], severity="return"),
    "BOOTLOOP": Diagnosis(
        "BOOTLOOP", "Phone didn't start after the flash",
        "It's stuck on the logo or restarting. Usually the firmware didn't fully install or is the wrong variant.",
        ["Hold Power + Volume Down for 10 seconds to force a restart.",
         "If it's still stuck, run the phone again - it will re-flash."]),
    "WRONG_MODEL": Diagnosis(
        "WRONG_MODEL", "Firmware doesn't match this phone",
        "The phone reports a different model than the firmware. Flashing it could brick the phone, so we stopped.",
        ["Check the model on the Download-mode screen and run the phone again."]),
    "FIRMWARE_NOT_FOUND": Diagnosis(
        "FIRMWARE_NOT_FOUND", "No firmware found on Samsung's servers",
        "Samsung doesn't publish firmware for this model/region combination (common for carrier and demo variants).",
        ["Try another region code (EUX, EUY, BTU, XEU; for US 'U1' models XAA).",
         "Or download the firmware yourself and drop it in the firmware folder."]),
    "POST_FLASH_NO_USB": Diagnosis(
        "POST_FLASH_NO_USB", "Couldn't reconnect after the flash",
        "USB debugging is off again after the wipe, or the phone isn't allowed yet.",
        ["Turn USB debugging back on and tap Allow.", CABLE_FIX]),
}


def known(code: str, log: list[str] | None = None) -> Diagnosis:
    base = KNOWN[code]
    return Diagnosis(base.code, base.title, base.cause, list(base.fix), list(log or []), base.severity)


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def relevant_lines(lines: list[str], n: int = 12) -> list[str]:
    """Prefer lines that mention errors; fall back to the last n lines."""
    errs = [l for l in lines if re.search(r"error|fail|denied|timeout|invalid|mismatch|refus|unable", l, re.I)]
    picked = errs[-n:] if errs else lines[-n:]
    return picked


def diagnose_download(lines: list[str]) -> Diagnosis:
    text = "\n".join(lines)
    log = relevant_lines(lines)
    if _has(text, r"os error 112", r"no space", r"not enough space", r"disk full"):
        return Diagnosis("DISK_FULL", "Not enough disk space",
                         "Firmware needs about 25 GB free (download + unpacked copy).",
                         ["Free up space, or delete old model folders in Documents\\GalaxyRefurb\\firmware.",
                          "Then press Retry."], log)
    if _has(text, r"timed? ?out", r"connection", r"dns", r"resolve", r"network", r"tls", r"reset by peer", r"os error 10060"):
        return Diagnosis("NETWORK", "Download interrupted",
                         "The internet connection dropped or Samsung's server was slow.",
                         ["Check the PC's internet connection.", "Press Retry - the download resumes where it stopped."], log)
    if _has(text, r"\b40[13]\b", r"unauthori[sz]ed", r"forbidden", r"nonce", r"status.?code"):
        return Diagnosis("SERVER_REFUSED", "Samsung's server refused the download",
                         "Samsung's update server rejected the request. This is usually temporary.",
                         ["Wait a few minutes and press Retry.",
                          "If it keeps failing, add the firmware by hand (Help > Firmware)."], log)
    return Diagnosis("DOWNLOAD_FAILED", "Firmware download failed",
                     "The download stopped with an error (see log lines).",
                     ["Press Retry.", "If it keeps failing, add the firmware by hand (Help > Firmware)."], log)


def diagnose_flash(lines: list[str]) -> Diagnosis:
    text = "\n".join(lines)
    log = relevant_lines(lines)
    if _has(text, r"md5", r"checksum"):
        return Diagnosis("CORRUPT_FIRMWARE", "Firmware file is damaged",
                         "A checksum didn't match - the download or unzip was corrupted.",
                         ["Delete this model's folder in Documents\\GalaxyRefurb\\firmware.",
                          "Run the phone again to download a fresh copy."], log)
    if _has(text, r"sw rev", r"rollback", r"binary", r"version.*(lower|older)"):
        return Diagnosis("ANTI_ROLLBACK", "Firmware is older than the phone allows",
                         "Samsung phones refuse firmware older than their current bootloader (anti-rollback). "
                         "The phone screen may say 'SW REV CHECK FAIL'.",
                         ["Delete this model's firmware folder and run again to get the newest version.",
                          "Hold Power + Volume Down to restart the phone - it is not damaged."], log)
    if _has(text, r"does not fit", r"too large", r"size check", r"partition.*not found", r"pit"):
        return Diagnosis("WRONG_FIRMWARE", "Firmware doesn't fit this phone",
                         "The firmware's partitions don't match the phone - likely a different variant.",
                         ["Check the model on the Download-mode screen.",
                          "Delete the firmware folder for this model and run again."], log)
    if _has(text, r"failed to detect", r"no .*device", r"not found"):
        return Diagnosis("NOT_IN_DOWNLOAD_MODE", "Phone not found in Download mode",
                         "The phone wasn't on the blue 'Downloading...' screen, or Windows couldn't talk to it.",
                         ["Put the phone in Download mode (steps shown) and press Retry.", DRIVER_FIX, CABLE_FIX], log)
    if _has(text, r"time ?out", r"timed out", r"failed to (send|receive|read|write)", r"handshake", r"disconnect",
            r"access is denied", r"com\d", r"broken pipe", r"pipe"):
        return Diagnosis("USB_CONNECTION", "USB connection dropped during the flash",
                         "Communication with the phone failed mid-flash. Almost always the cable, the port or the driver.",
                         [CABLE_FIX, "Try a different USB port (rear ports on desktops).", DRIVER_FIX,
                          "The phone is safe - it stays in Download mode. Press Retry."], log)
    return Diagnosis("FLASH_FAILED", "Flash failed",
                     "The flashing tool stopped with an error (see log lines). The phone is safe to flash again.",
                     ["Press Retry.", "If it fails twice, choose 'Use Odin instead'."], log)


def diagnose_verify(problems: list[str]) -> Diagnosis:
    text = " ".join(problems)
    fixes = []
    if "managed" in text.lower():
        fixes.append("Still managed by the store: return to the seller to release it from Knox.")
    if "demo" in text.lower():
        fixes.append("Still demo software: this unit may be a permanent demo (LDU). Try flashing again; if it "
                     "persists, sell as 'display unit' or for parts.")
    if "imei" in text.lower():
        fixes.append("No IMEI: typical of hard demo units. Can't be sold as a working phone.")
    if "account" in text.lower():
        fixes.append("Remove the signed-in account in Settings > Accounts, then run the final check again.")
    if "battery" in text.lower():
        fixes.append("Battery problem: replace the battery or disclose it in the listing.")
    if "hardware" in text.lower() or "call" in text.lower():
        fixes.append("Note the fault in your listing or repair before selling.")
    return Diagnosis("CHECKS_FAILED", "Some final checks failed",
                     "The phone was flashed, but these checks didn't pass: " + "; ".join(problems),
                     fixes or ["See the failed checks above."], problems)
