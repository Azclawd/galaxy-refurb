"""inventory.csv - one row per phone, updated at each stage. Opens in Excel."""
from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path

COLUMNS = ["updated", "serial", "model", "region", "imei", "firmware_before", "triage_verdict",
           "imei_check", "final_result", "notes", "log_file"]


def load(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def record(path: Path, **fields: str) -> None:
    """Insert or update the row for this phone (matched on serial, else IMEI)."""
    rows = load(path)
    key_serial = fields.get("serial", "")
    key_imei = fields.get("imei", "")
    target = None
    for row in rows:
        if (key_serial and row.get("serial") == key_serial) or (not key_serial and key_imei and row.get("imei") == key_imei):
            target = row
            break
    if target is None:
        target = {c: "" for c in COLUMNS}
        rows.append(target)
    for k, v in fields.items():
        if k in COLUMNS and v not in (None, ""):
            target[k] = str(v)
    target["updated"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    try:
        tmp.replace(path)
    except PermissionError:
        # Excel has the file open. Keep the update next to it rather than losing it.
        alt = path.with_name(path.stem + "-pending.csv")
        tmp.replace(alt)
        print(f"inventory.csv is open in another program - saved to {alt.name} instead. Close Excel to fix.")
