"""Tests using synthetic phone outputs (no phone needed)."""
from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from galaxy_refurb import final_check, firmware, flash, inventory, triage
from galaxy_refurb.adb import parse_devices, parse_getprop

GETPROP_STORE = """\
[ro.product.model]: [SM-S921B]
[ro.product.name]: [e1sxeea]
[ro.boot.serialno]: [R5CX12345AB]
[ro.build.PDA]: [S921BXXU3AXH7]
[ro.csc.sales_code]: [BTU]
[ro.build.version.release]: [14]
[ro.boot.warranty_bit]: [0]
[gsm.sim.state]: [ABSENT,ABSENT]
"""

GETPROP_LDU = GETPROP_STORE.replace("[e1sxeea]", "[e1sxeea_ldu]")

PACKAGES_STORE = """\
package:com.android.settings
package:com.samsung.sea.retailagent
package:com.samsung.android.app.retaildemo
package:com.google.android.gms
"""

DEVICE_POLICY_OWNED = """\
Current Device Policy Manager state:
  Immutable state:
    mHasFeature=true
  Device Owner:
    admin=ComponentInfo{com.samsung.android.knox.containeragent/com.samsung.klms.Receiver}
    name=Knox
    package=com.samsung.android.knox.containeragent
    isOrganizationOwnedDevice=true
"""

DEVICE_POLICY_CLEAN = """\
Current Device Policy Manager state:
  Immutable state:
    mHasFeature=true
"""

ACCOUNTS_TWO = """\
User UserInfo{0:Owner:c13}:
  Accounts: 3
    Account {name=store.demo@gmail.com, type=com.google}
    Account {name=store.demo@gmail.com, type=com.google}
    Account {name=demo@samsung.com, type=com.osp.app.signin}

  AccountId, Action_Type, timestamp, UID, TableName, Key
    Account {name=old@gmail.com, type=com.google}
"""

ACCOUNTS_NONE = """\
User UserInfo{0:Owner:c13}:
  Accounts: 0

"""


def raw(props=GETPROP_STORE, packages=PACKAGES_STORE, policy=DEVICE_POLICY_OWNED, accounts=ACCOUNTS_NONE):
    return triage.RawData(parse_getprop(props), triage.parse_packages(packages), policy, accounts)


# ---------------------------------------------------------------- adb parsing

def test_parse_getprop():
    p = parse_getprop(GETPROP_STORE)
    assert p["ro.product.model"] == "SM-S921B"
    assert p["gsm.sim.state"] == "ABSENT,ABSENT"


def test_parse_devices():
    text = "List of devices attached\nR5CX12345AB\tdevice\nABC\tunauthorized\n\n"
    assert parse_devices(text) == [("R5CX12345AB", "device"), ("ABC", "unauthorized")]


# ---------------------------------------------------------------- triage

def test_store_phone_ready_to_flash_with_warnings():
    r = triage.analyze(raw())
    assert r.model == "SM-S921B"
    assert r.region == "BTU"
    assert r.serial == "R5CX12345AB"
    assert r.device_owner == "com.samsung.android.knox.containeragent"
    assert "com.samsung.sea.retailagent" in r.retail_packages
    assert "com.google.android.gms" not in r.retail_packages
    assert r.verdict == triage.READY
    assert any("Knox Mobile Enrollment" in x for x in r.reasons)


def test_accounts_block_flash():
    r = triage.analyze(raw(accounts=ACCOUNTS_TWO))
    assert r.google_accounts == 1  # duplicate + history entry not double counted
    assert r.samsung_accounts == 1
    assert r.verdict == triage.REMOVE_ACCOUNTS


def test_blocked_imei_returns():
    r = triage.analyze(raw(accounts=ACCOUNTS_TWO))
    r.imei_check = "blocked"
    triage.decide(r)
    assert r.verdict == triage.RETURN


def test_ldu_detected():
    r = triage.analyze(raw(props=GETPROP_LDU))
    assert r.demo_markers and "ldu" in r.demo_markers[0]


def test_inline_device_owner():
    owner, profile = triage.parse_device_owner("  Device Owner: admin=ComponentInfo{com.mdm/.R} name=x\n")
    assert owner == "com.mdm" and profile == ""


def test_clean_policy_no_owner():
    assert triage.parse_device_owner(DEVICE_POLICY_CLEAN) == ("", "")


def test_imei_validation():
    assert triage.valid_imei("490154203237518")
    assert not triage.valid_imei("490154203237519")
    assert not triage.valid_imei("12345")
    assert triage.mask_imei("490154203237518") == "49xxxxxxxxx7518"


# ---------------------------------------------------------------- final check

def test_final_check_clean_phone():
    before = triage.TriageResult(model="SM-S921B")
    problems, notes = final_check.evaluate(
        raw(packages="package:com.android.settings\n", policy=DEVICE_POLICY_CLEAN), before)
    assert problems == []
    assert any("No SIM" in n for n in notes)


def test_final_check_still_dirty():
    before = triage.TriageResult(model="SM-S921B")
    problems, _ = final_check.evaluate(raw(props=GETPROP_LDU, accounts=ACCOUNTS_TWO), before)
    text = " ".join(problems)
    assert "Still managed" in text and "demo" in text and "Accounts" in text


def test_battery_parse():
    assert final_check.parse_battery("Current Battery Service state:\n  level: 87\n  health: 2\n") == ("87", "Good")


# ---------------------------------------------------------------- flashing (samloader-rs)

DETECT_OUT = """\
Device detected (Download mode)
  Model:               SM-S921B
  Serial Number:       R5CX12345AB
  Bootloader Version:  S921BXXU3AXH7
  Sales Code:          BTU
  Storage:             256 GB SAMSUNG
"""


def test_parse_detect():
    info = flash.parse_detect(DETECT_OUT)
    assert (info.model, info.serial, info.sales_code) == ("SM-S921B", "R5CX12345AB", "BTU")
    assert flash.parse_detect("Device detected (Download mode)\n").model == ""


def test_flash_command_uses_csc_not_home_csc(tmp_path):
    zpath = _make_firmware_zip(tmp_path)
    tars = firmware.find_tars(firmware.firmware_folder(zpath, tmp_path / "work"))
    cmd = flash.flash_command("samloader.exe", tars)
    assert cmd[:3] == ["samloader.exe", "flash", "--wait"]
    flags = cmd[3::2]
    assert flags == ["--BL", "--AP", "--CP", "--CSC"]
    assert all("HOME_CSC" not in x for x in cmd)


# ---------------------------------------------------------------- firmware

def _make_tar_md5(path: Path, files: dict[str, bytes]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    path.write_bytes(buf.getvalue() + b"0123456789abcdef0123456789abcdef  " + path.name.encode() + b"\n")


def _make_firmware_zip(tmp_path: Path, name: str = "SM-S921B_BTU_S921BXXU3_fac.zip", dest: Path | None = None) -> Path:
    src = tmp_path / ("src-" + name)
    src.mkdir(parents=True)
    _make_tar_md5(src / "BL_S921BXXU3.tar.md5", {"sboot.bin.lz4": b"SBOOT"})
    _make_tar_md5(src / "AP_S921BXXU3.tar.md5", {"boot.img.lz4": b"BOOT", "super.img.lz4": b"SUPER"})
    _make_tar_md5(src / "CP_S921BXXU3.tar.md5", {"modem.bin.lz4": b"MODEM"})
    _make_tar_md5(src / "CSC_OXM_S921BOXM3.tar.md5", {"cache.img.lz4": b"CACHE"})
    _make_tar_md5(src / "HOME_CSC_OXM_S921BOXM3.tar.md5", {"cache.img.lz4": b"HOME"})
    fw = dest or (tmp_path / "fw")
    fw.mkdir(parents=True, exist_ok=True)
    zpath = fw / name
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in src.iterdir():
            zf.write(p, p.name)
    return zpath


def test_find_and_unpack_firmware(tmp_path):
    zpath = _make_firmware_zip(tmp_path)
    found = firmware.find_local_firmware(zpath.parent, "SM-S921B")
    assert found == zpath
    assert firmware.find_local_firmware(zpath.parent, "SM-A156B") is None

    folder = firmware.firmware_folder(found, tmp_path / "work")
    tars = firmware.find_tars(folder)
    assert set(tars) == {"BL", "AP", "CP", "CSC"}
    assert tars["CSC"].name.startswith("CSC_")  # never HOME_CSC
    # second call reuses the unpacked cache
    assert firmware.firmware_folder(found, tmp_path / "work") == folder


def test_region_specific_download_folder_preferred(tmp_path):
    fw = tmp_path / "fw"
    btu = _make_firmware_zip(tmp_path, "SM-S921B_2_btu_fac.zip", fw / "SM-S921B" / "BTU")
    _make_firmware_zip(tmp_path, "SM-S921B_2_eux_fac.zip", fw / "SM-S921B" / "EUX")
    assert firmware.find_local_firmware(fw, "SM-S921B", "BTU") == btu
    assert firmware.find_local_firmware(fw, "sm-s921b", "eux").parent.name == "EUX"


def test_model_folder_loose_tars(tmp_path):
    model_dir = tmp_path / "SM-A156B"
    model_dir.mkdir()
    _make_tar_md5(model_dir / "AP_A156B.tar.md5", {"boot.img": b"x"})
    assert firmware.find_local_firmware(tmp_path, "SM-A156B") == model_dir


def test_download_command():
    cmd = firmware.download_command("samloader.exe", "sm-s921b", "btu", Path("out"), 8)
    assert cmd == ["samloader.exe", "download", "-m", "SM-S921B", "-r", "BTU", "-d", "out", "-j", "8"]


# ---------------------------------------------------------------- inventory

def test_inventory_upsert(tmp_path):
    path = tmp_path / "inventory.csv"
    inventory.record(path, serial="R5C1", model="SM-S921B", triage_verdict="READY_TO_FLASH")
    inventory.record(path, serial="R5C2", model="SM-A156B")
    inventory.record(path, serial="R5C1", final_result="PASS_READY_TO_SELL")
    rows = inventory.load(path)
    assert len(rows) == 2
    assert rows[0]["model"] == "SM-S921B"
    assert rows[0]["triage_verdict"] == "READY_TO_FLASH"
    assert rows[0]["final_result"] == "PASS_READY_TO_SELL"


def test_regions_to_try():
    assert firmware.regions_to_try("btu") == ["BTU", "EUX", "EUY"]
    assert firmware.regions_to_try("EUX") == ["EUX", "EUY"]
    assert firmware.regions_to_try("") == ["EUX", "EUY"]


def test_btu_phone_finds_eux_download(tmp_path):
    fw = tmp_path / "fw"
    eux = _make_firmware_zip(tmp_path, "SM-S921B_2_eux_fac.zip", fw / "SM-S921B" / "EUX")
    assert firmware.find_local_firmware(fw, "SM-S921B", "BTU") == eux


def test_manual_triage_note_survives_redecide():
    r = triage.TriageResult(model="SM-A156B", region="BTU", manual=True)
    triage.decide(r)
    triage.decide(r)
    assert sum("Download mode" in x for x in r.reasons) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
