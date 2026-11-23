"""Read a phone over ADB and decide what it needs.

Collection (needs a phone) is separated from analysis (pure functions) so the
analysis can be unit-tested with captured outputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .adb import Adb

# Package name fragments that indicate store/demo software.
RETAIL_PACKAGE_HINTS = ("retail", "demo", ".rdu", "rdu.", "storemode", "shopdemo", ".ldu", "ldu.")

# Props where demo (LDU) firmware tends to show itself.
DEMO_PROP_KEYS = (
    "ro.product.name", "ro.product.device", "ro.build.characteristics",
    "ro.csc.sales_code", "ro.build.display.id", "ro.build.PDA", "ro.boot.carrierid",
    "ro.product.vendor.name", "ro.product.system.name",
)
DEMO_PROP_HINTS = ("ldu", "demo", "rdu", "retail")

# Account types that cause activation locks after a reset.
GOOGLE_ACCOUNT_TYPE = "com.google"
SAMSUNG_ACCOUNT_TYPE = "com.osp.app.signin"

# Verdicts
READY = "READY_TO_FLASH"
REMOVE_ACCOUNTS = "REMOVE_ACCOUNTS_FIRST"
RETURN = "RETURN_TO_SELLER"


@dataclass
class RawData:
    props: dict[str, str]
    packages: list[str]
    device_policy: str
    accounts: str


@dataclass
class TriageResult:
    model: str = ""
    serial: str = ""
    firmware: str = ""
    region: str = ""
    android: str = ""
    product_name: str = ""
    warranty_bit: str = ""
    device_owner: str = ""
    profile_owner: str = ""
    retail_packages: list[str] = field(default_factory=list)
    google_accounts: int = 0
    samsung_accounts: int = 0
    demo_markers: list[str] = field(default_factory=list)
    imei: str = ""
    imei_check: str = "not checked"   # "clean" / "blocked" / "not checked"
    manual: bool = False   # triaged from Download mode (no ADB) - accounts/management unknown
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)


def parse_packages(text: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("package:")]


def parse_device_owner(device_policy: str) -> tuple[str, str]:
    """Return (device_owner_package, profile_owner_package) from `dumpsys device_policy`."""
    def find(label: str) -> str:
        # Block form:  "Device Owner: \n    admin=ComponentInfo{pkg/cls}"
        m = re.search(label + r":[^\n]*\n(?:[ \t]+[^\n]*\n){0,4}?[ \t]+admin=ComponentInfo\{([^/}]+)", device_policy)
        if m:
            return m.group(1)
        # Inline form: "Device Owner: admin=ComponentInfo{pkg/cls} ..." or "package=pkg"
        m = re.search(label + r":[^\n]*?(?:ComponentInfo\{([^/}]+)|package=([\w.]+))", device_policy)
        if m:
            return m.group(1) or m.group(2)
        return ""

    return find("Device Owner"), find("Profile Owner")


def count_accounts(accounts_dump: str, account_type: str) -> int:
    """Count distinct accounts of a type in `dumpsys account` output.

    Only the "Accounts:" section is used where present, so history entries are not counted.
    """
    section = accounts_dump
    m = re.search(r"Accounts:\s*\d+\s*\n(.*?)(?:\n\s*\n|\Z)", accounts_dump, re.DOTALL)
    if m:
        section = m.group(1)
    names = set(re.findall(r"Account \{name=([^,]+), type=" + re.escape(account_type) + r"\}", section))
    return len(names)


def find_retail_packages(packages: list[str]) -> list[str]:
    return sorted(p for p in packages if any(h in p.lower() for h in RETAIL_PACKAGE_HINTS))


def find_demo_markers(props: dict[str, str]) -> list[str]:
    markers = []
    for key in DEMO_PROP_KEYS:
        val = props.get(key, "")
        if any(h in val.lower() for h in DEMO_PROP_HINTS):
            markers.append(f"{key}={val}")
    return markers


def region_code(props: dict[str, str]) -> str:
    for key in ("ro.csc.sales_code", "persist.sys.omc_salescode", "ro.boot.sales_code", "ril.sales_code"):
        if props.get(key):
            return props[key].strip().upper()
    return ""


def analyze(raw: RawData) -> TriageResult:
    p = raw.props
    r = TriageResult(
        model=p.get("ro.product.model", "").strip(),
        serial=(p.get("ril.serialnumber") or p.get("ro.boot.serialno") or p.get("ro.serialno") or "").strip(),
        firmware=p.get("ro.build.PDA", "") or p.get("ro.build.display.id", ""),
        region=region_code(p),
        android=p.get("ro.build.version.release", ""),
        product_name=p.get("ro.product.name", ""),
        warranty_bit=p.get("ro.boot.warranty_bit", ""),
    )
    r.device_owner, r.profile_owner = parse_device_owner(raw.device_policy)
    r.retail_packages = find_retail_packages(raw.packages)
    r.google_accounts = count_accounts(raw.accounts, GOOGLE_ACCOUNT_TYPE)
    r.samsung_accounts = count_accounts(raw.accounts, SAMSUNG_ACCOUNT_TYPE)
    r.demo_markers = find_demo_markers(p)
    decide(r)
    return r


def decide(r: TriageResult) -> None:
    """Set verdict + reasons. Safe to call again after IMEI check / account removal."""
    reasons: list[str] = []
    verdict = READY

    if r.manual:
        reasons.append("Triaged from Download mode - accounts and store management could not be checked. "
                       "The final check will catch any lock.")
    if r.imei_check == "blocked":
        verdict = RETURN
        reasons.append("IMEI is reported lost/stolen/blocked. Do not refurbish or sell. Return to seller.")
    if r.google_accounts or r.samsung_accounts:
        if verdict != RETURN:
            verdict = REMOVE_ACCOUNTS
        reasons.append(
            f"Signed-in accounts: {r.google_accounts} Google, {r.samsung_accounts} Samsung. "
            "These cause an activation lock after reset - remove them in Settings first.")
    if r.device_owner:
        reasons.append(
            f"Phone is managed by '{r.device_owner}' (store/Knox management). Flashing removes it, "
            "BUT if the IMEI is registered in Knox Mobile Enrollment it will lock again at setup.")
    if r.profile_owner:
        reasons.append(f"Work profile managed by '{r.profile_owner}'. Removed by flashing.")
    if r.retail_packages:
        reasons.append(f"Store/demo apps found: {', '.join(r.retail_packages)}. Removed by flashing.")
    if r.demo_markers:
        reasons.append(
            "Looks like demo (LDU) firmware: " + "; ".join(r.demo_markers)
            + ". Flashing retail firmware usually converts it - check IMEI and calling afterwards.")
    if r.warranty_bit and r.warranty_bit not in ("0", "0x0"):
        reasons.append(f"Knox warranty bit is tripped ({r.warranty_bit}). Phone works, but Samsung Pay/"
                       "Secure Folder won't. Disclose when selling.")
    if not r.model:
        reasons.append("Could not read model number - you'll be asked for it.")
    if not r.region:
        reasons.append("Could not read region (CSC) code - you'll be asked for it.")
    if verdict == READY and not reasons:
        reasons.append("Nothing unusual found.")
    r.verdict = verdict
    r.reasons = reasons


def collect(adb: Adb, serial: str) -> RawData:
    props = adb.getprop(serial)
    packages = parse_packages(adb.shell(serial, "pm list packages"))
    device_policy = adb.shell(serial, "dumpsys device_policy")
    accounts = adb.shell(serial, "dumpsys account")
    return RawData(props=props, packages=packages, device_policy=device_policy, accounts=accounts)


def valid_imei(imei: str) -> bool:
    """15 digits with a valid Luhn check digit."""
    if not re.fullmatch(r"\d{15}", imei):
        return False
    total = 0
    for i, ch in enumerate(imei):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_imei(imei: str) -> str:
    if len(imei) < 6:
        return imei
    return imei[:2] + "x" * (len(imei) - 6) + imei[-4:]
