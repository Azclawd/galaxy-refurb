"""Find, download and unpack official Samsung firmware.

Samsung firmware comes as a zip holding five packages:
    BL_*.tar.md5   bootloader
    AP_*.tar.md5   system (the big one)
    CP_*.tar.md5   modem
    CSC_*.tar.md5  region settings + full wipe   <- we use this one
    HOME_CSC_*     region settings, keeps data   <- never used here

Downloads use samloader-rs (https://github.com/topjohnwu/samloader-rs), which talks to
Samsung's own update servers and decrypts on the fly, so the result is the official zip.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from .log import log

COMPONENTS = ("BL", "AP", "CP", "CSC")


def short_model(model: str) -> str:
    """SM-S921B -> S921B"""
    return re.sub(r"^SM-", "", model.strip().upper())


def find_tars(folder: Path, recursive: bool = True) -> dict[str, Path]:
    """Map BL/AP/CP/CSC to their .tar(.md5) files in folder. HOME_CSC is ignored."""
    found: dict[str, Path] = {}
    for p in sorted(folder.rglob("*.tar*") if recursive else folder.glob("*.tar*")):
        name = p.name.upper()
        if not (name.endswith(".TAR") or name.endswith(".TAR.MD5")):
            continue
        if name.startswith("HOME_CSC"):
            continue
        for comp in COMPONENTS:
            if name.startswith(comp + "_"):
                found.setdefault(comp, p)
    return found


def _candidates_in(folder: Path) -> list[Path]:
    out: list[Path] = sorted(p for p in folder.glob("*.zip") if p.is_file())
    if find_tars(folder, recursive=False).get("AP"):
        out.append(folder)
    return out


def find_local_firmware(firmware_dir: Path, model: str, region: str = "") -> Path | None:
    """Look for a firmware zip or unpacked folder for this model, best match first.

      firmware/<MODEL>/<REGION>/   (where automatic downloads go)
      firmware/<MODEL>/            (zip or loose .tar.md5 files you put there)
      firmware/*<MODEL>*.zip       (e.g. SM-S921B_BTU_S921BXXU3....zip)
    """
    sm = short_model(model)
    if not sm or not firmware_dir.exists():
        return None
    model_dir = firmware_dir / model.strip().upper()
    candidates: list[Path] = []
    if region and (model_dir / region.upper()).is_dir():
        candidates += _candidates_in(model_dir / region.upper())
    if model_dir.is_dir():
        candidates += _candidates_in(model_dir)
        if not region:
            for sub in sorted(p for p in model_dir.iterdir() if p.is_dir()):
                candidates += _candidates_in(sub)
    for p in sorted(firmware_dir.iterdir()):
        if p == model_dir or sm not in p.name.upper():
            continue
        if p.is_file() and p.suffix.lower() == ".zip":
            candidates.append(p)
        elif p.is_dir() and find_tars(p).get("AP"):
            candidates.append(p)
    return candidates[0] if candidates else None


def download_command(samloader: str, model: str, region: str, out_dir: Path, threads: int = 8) -> list[str]:
    return [samloader, "download", "-m", model.upper(), "-r", region.upper(), "-d", str(out_dir), "-j", str(threads)]


def download_firmware(samloader: str | None, settings: dict, firmware_dir: Path, model: str, region: str,
                      echo=print) -> Path | None:
    """Download the latest official firmware. Returns the zip path, or None on failure."""
    if not samloader or not settings.get("firmware_download_enabled", True) or not model or not region:
        return None
    out_dir = firmware_dir / model.upper() / region.upper()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = download_command(samloader, model, region, out_dir, int(settings.get("download_threads", 8)))
    log("$ " + " ".join(cmd))
    try:
        # Progress bar goes straight to the console so you can watch it.
        proc = subprocess.run(cmd, timeout=8 * 3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"download failed: {exc!r}")
        echo(f"Download failed: {exc}")
        return None
    log(f"exit {proc.returncode}")
    if proc.returncode != 0:
        return None
    zips = sorted(out_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def unpack_zip(zip_path: Path, dest: Path) -> Path:
    """Extract a firmware zip into dest (cleared first). Returns dest."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    return dest


def firmware_folder(source: Path, work_dir: Path) -> Path:
    """Return a folder containing the .tar.md5 files, unpacking a zip if needed.

    Unpacked folders are cached next to the zip (<zip>.unpacked) so the next phone of the
    same model doesn't wait for another unzip.
    """
    if source.is_dir():
        return source
    cache = source.with_name(source.stem + ".unpacked")
    if cache.is_dir() and set(COMPONENTS) <= set(find_tars(cache)):
        return cache
    return unpack_zip(source, cache)
