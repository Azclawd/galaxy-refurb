"""Tests for the app-window pieces: diagnosis, sessions/prompts, and a full simulated run."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from galaxy_refurb import diagnose, flash, inventory, workflow
from galaxy_refurb.config import Config
from galaxy_refurb.devices import classify_usb, parse_pnp_json
from galaxy_refurb.session import Cancelled, Session

from test_core import GETPROP_STORE, _make_firmware_zip

# ---------------------------------------------------------------- diagnosis


@pytest.mark.parametrize("lines,code", [
    (["Uploading AP", "ERROR: MD5 checksum mismatch for AP.tar.md5"], "CORRUPT_FIRMWARE"),
    (["ERROR: Failed to send data: timed out"], "USB_CONNECTION"),
    (["ERROR: Failed to detect compatible download-mode device."], "NOT_IN_DOWNLOAD_MODE"),
    (["SW REV CHECK FAIL: binary 3 < device 5"], "ANTI_ROLLBACK"),
    (["ERROR: something odd"], "FLASH_FAILED"),
])
def test_diagnose_flash(lines, code):
    d = diagnose.diagnose_flash(lines)
    assert d.code == code and d.fix and d.log


@pytest.mark.parametrize("lines,code", [
    (["ERROR: Download failed: os error 112"], "DISK_FULL"),
    (["ERROR: Download failed: connection reset by peer"], "NETWORK"),
    (["ERROR: status code 403"], "SERVER_REFUSED"),
    (["weird"], "DOWNLOAD_FAILED"),
])
def test_diagnose_download(lines, code):
    assert diagnose.diagnose_download(lines).code == code


def test_known_return_severity():
    assert diagnose.known("KNOX_ENROLLED").severity == "return"
    assert diagnose.known("BOOTLOOP").severity == "error"


def test_relevant_lines_prefers_errors():
    lines = [f"line {i}" for i in range(50)] + ["ERROR: boom"]
    assert diagnose.relevant_lines(lines) == ["ERROR: boom"]


# ---------------------------------------------------------------- device detection helpers

def test_usb_classification():
    assert classify_usb([]) is None
    assert classify_usb(parse_pnp_json('{"Name":"SAMSUNG Mobile USB","ConfigManagerErrorCode":0}')).state == "samsung_no_debug"
    assert classify_usb(parse_pnp_json('[{"Name":"x","ConfigManagerErrorCode":28}]')).state == "no_driver"


def test_imei_from_service_call():
    out = ("Result: Parcel(\n  0x00000000: 00000000 0000000f 00390034 00310030 '........4.9.0.1.'\n"
           "  0x00000010: 00340035 00300032 00320033 00370033 '5.4.2.0.3.2.3.7.'\n"
           "  0x00000020: 00310035 00000038                   '5.1.8...        ')\n")
    assert workflow.parse_service_call_imei(out) == "490154203237518"
    assert workflow.parse_service_call_imei("Result: Parcel(ffffffff 'error')") == ""


# ---------------------------------------------------------------- session prompts

def test_prompt_answer_roundtrip():
    s = Session("t", "d")
    pid = s.show_prompt("choice", "Q?", options=[{"value": "a", "label": "A"}])
    assert s.snapshot()["prompt"]["id"] == pid
    assert s.answer(pid, "a")
    assert s.poll_answer(pid, 0.1) == (True, "a")
    assert s.snapshot()["prompt"] is None
    assert not s.answer(pid, "again")


def test_cancel_unblocks_prompt():
    s = Session("t", "d")
    pid = s.show_prompt("choice", "Q?")
    threading.Timer(0.2, s.request_cancel).start()
    with pytest.raises(Cancelled):
        s.poll_answer(pid, 2)


def test_long_poll_returns_on_change():
    s = Session("t", "d")
    v = s.snapshot()["version"]
    threading.Timer(0.2, lambda: s.add_log("hi")).start()
    t = time.time()
    snap = s.wait_for_change(v, 5)
    assert snap["version"] > v and time.time() - t < 3


# ---------------------------------------------------------------- full simulated run

class FakeAdb:
    """Store phone before the flash, clean phone after."""

    def __init__(self):
        self.flashed = False
        self.connected = True

    def devices(self):
        return [("R5CX12345AB", "device")] if self.connected else []

    def shell(self, serial, cmd, timeout=60):
        if cmd == "getprop":
            return GETPROP_STORE
        if cmd.startswith("pm list packages"):
            return "package:com.android.settings\n" + ("" if self.flashed else "package:com.samsung.sea.retailagent\n")
        if "device_policy" in cmd:
            return "" if self.flashed else "  Device Owner:\n    admin=ComponentInfo{com.knox.retail/.R}\n"
        if "dumpsys account" in cmd:
            return "  Accounts: 0\n\n"
        if "dumpsys battery" in cmd:
            return "  level: 90\n  health: 2\n"
        if "iphonesubinfo" in cmd:
            return ("  0x00000000: 00000000 0000000f 00390034 00310030 '........4.9.0.1.'\n"
                    "  0x00000010: 00340035 00300032 00320033 00370033 '5.4.2.0.3.2.3.7.'\n"
                    "  0x00000020: 00310035 00000038                   '5.1.8...        '\n")
        return ""

    def getprop(self, serial):
        from galaxy_refurb.adb import parse_getprop
        return parse_getprop(GETPROP_STORE)

    def reboot_download(self, serial):
        self.connected = False


def run_job(tmp_path, monkeypatch, answers: dict[str, str], flash_ok: bool = True):
    adb = FakeAdb()
    cfg = Config(app_home=tmp_path / "home", data_home=tmp_path / "data")
    cfg.ensure_dirs()
    _make_firmware_zip(tmp_path, "SM-S921B_2_x_fac.zip", cfg.firmware_dir / "SM-S921B" / "EUX")
    (cfg.tools_dir / "samloader").mkdir(parents=True)
    (cfg.tools_dir / "samloader" / "samloader").write_text("")  # presence only; calls are faked

    monkeypatch.setattr(flash.Samloader, "detect",
                        lambda self: None if adb.connected else flash.DownloadModeInfo("SM-S921B", "R5CX12345AB", "BTU"))

    def fake_run_stream(cmd, on_line=None, timeout=None, keep=60):
        for pct in (20, 60, 100):
            on_line and on_line(f"Uploading AP {pct}%")
        if flash_ok:
            adb.flashed, adb.connected = True, True
            return 0, ["done"]
        return 1, ["ERROR: Failed to send data: timed out"]

    monkeypatch.setattr(workflow, "run_stream", fake_run_stream)
    monkeypatch.setattr(workflow.time, "sleep", lambda s: None)

    s = Session("t", str(cfg.data_home))
    seen: list[str] = []

    def answerer():
        while True:
            snap = s.snapshot()
            if snap["job"] and snap["job"]["result"]:
                return
            p = snap["prompt"]
            if p:
                for frag, val in answers.items():
                    if frag.lower() in p["title"].lower():
                        seen.append(p["title"])
                        s.answer(p["id"], val)
                        break
            time.sleep(0.01)

    t = threading.Thread(target=answerer, daemon=True)
    t.start()
    workflow.Workflow(cfg, s, adb).run()  # type: ignore[arg-type]
    t.join(2)
    return s.snapshot()["job"], seen, cfg


ANSWERS = {"IMEI clean": "clean", "Erase": "go", "Set up": "ok", "SIM": "ok", "hardware": "ok", "reset": "done",
           "Problem": "stop", "USB": "stop", "connection": "stop"}


def test_full_run_passes(tmp_path, monkeypatch):
    job, seen, cfg = run_job(tmp_path, monkeypatch, ANSWERS)
    assert job["result"]["status"] == "pass", job["result"]
    assert all(st["status"] == "done" for st in job["steps"]), [(s["key"], s["status"]) for s in job["steps"]]
    verify = next(st for st in job["steps"] if st["key"] == "verify")
    assert all(c["status"] == "done" for c in verify["checks"])
    row = inventory.load(cfg.inventory_csv)[0]
    assert row["final_result"] == "PASS_READY_TO_SELL" and row["imei"] == "490154203237518"


def test_knox_at_setup_returns_to_seller(tmp_path, monkeypatch):
    job, _, cfg = run_job(tmp_path, monkeypatch, {**ANSWERS, "Set up": "knox"})
    assert job["result"]["status"] == "return"
    assert job["result"]["diagnosis"]["code"] == "KNOX_ENROLLED"
    assert inventory.load(cfg.inventory_csv)[0]["final_result"] == "RETURN_TO_SELLER"


def test_flash_failure_shows_diagnosis(tmp_path, monkeypatch):
    job, seen, _ = run_job(tmp_path, monkeypatch, ANSWERS, flash_ok=False)
    assert job["result"]["status"] == "fail"
    assert job["result"]["diagnosis"]["code"] == "USB_CONNECTION"
    assert next(st for st in job["steps"] if st["key"] == "flash")["status"] == "failed"
