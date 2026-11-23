"""Paths and settings.

Install layout on Windows (created by install.ps1):
    %LOCALAPPDATA%\\GalaxyRefurb\\            app home (venv, tools, settings.json)
    %LOCALAPPDATA%\\GalaxyRefurb\\tools\\     adb (platform-tools), samloader, odin (optional)
    Documents\\GalaxyRefurb\\                 your data: inventory.csv, firmware, logs
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

DEFAULT_SETTINGS = {
    # "samloader" = flash automatically with samloader-rs, fall back to guided Odin if it fails.
    # "odin"      = always use guided Odin.
    "flash_backend": "samloader",
    # Download firmware automatically from Samsung's update servers (via samloader-rs).
    "firmware_download_enabled": True,
    "download_threads": 8,
    "download_mode_wait_seconds": 90,
    "first_boot_wait_minutes": 15,
}


def _app_home() -> Path:
    if os.environ.get("GALAXY_REFURB_HOME"):
        return Path(os.environ["GALAXY_REFURB_HOME"])
    if IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "GalaxyRefurb"
    return Path.home() / ".galaxy-refurb"


def _data_home() -> Path:
    if os.environ.get("GALAXY_REFURB_DATA"):
        return Path(os.environ["GALAXY_REFURB_DATA"])
    return Path.home() / "Documents" / "GalaxyRefurb"


@dataclass
class Config:
    app_home: Path = field(default_factory=_app_home)
    data_home: Path = field(default_factory=_data_home)
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))

    @property
    def tools_dir(self) -> Path:
        return self.app_home / "tools"

    @property
    def firmware_dir(self) -> Path:
        return self.data_home / "firmware"

    @property
    def work_dir(self) -> Path:
        return self.data_home / "work"

    @property
    def logs_dir(self) -> Path:
        return self.data_home / "logs"

    @property
    def inventory_csv(self) -> Path:
        return self.data_home / "inventory.csv"

    def ensure_dirs(self) -> None:
        for d in (self.data_home, self.firmware_dir, self.work_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _tool(self, names: list[str], subdirs: list[str]) -> str | None:
        for sub in subdirs:
            for name in names:
                p = self.tools_dir / sub / name
                if p.exists():
                    return str(p)
        for name in names:
            found = shutil.which(name)
            if found:
                return found
        return None

    @property
    def adb(self) -> str | None:
        return self._tool(["adb.exe", "adb"], ["platform-tools"])

    @property
    def samloader(self) -> str | None:
        return self._tool(["samloader.exe", "samloader"], ["samloader"])

    @property
    def odin(self) -> str | None:
        odin_dir = self.tools_dir / "odin"
        if odin_dir.exists():
            for p in sorted(odin_dir.rglob("Odin*.exe")):
                return str(p)
        return None


def load_config() -> Config:
    cfg = Config()
    path = cfg.app_home / "settings.json"
    if path.exists():
        try:
            cfg.settings.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"Warning: could not read {path}: {exc}. Using defaults.")
    cfg.ensure_dirs()
    return cfg
