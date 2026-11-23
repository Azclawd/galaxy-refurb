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
        # Other region folders, e.g. EUX firmware for a phone that reports BTU (EUX includes BTU).
        for sub in sorted(p for p in model_dir.iterdir() if p.is_dir() and not p.name.endswith(".unpacked")):
            if sub.name != region.upper():
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


# Samsung's servers often only serve the multi-region package: a UK phone reports BTU but its
# firmware is published under EUX (which contains BTU). Verified against Samsung's servers in CI.
FALLBACK_REGIONS = ("EUX", "EUY")


def regions_to_try(region: str) -> list[str]:
    out: list[str] = []
    for r in (region.strip().upper(), *FALLBACK_REGIONS):
        if r and r not in out:
            out.append(r)
    return out


def check_update(samloader: str, model: str, region: str) -> str | None:
    """Latest firmware version string for model/region, or None if Samsung has none."""
    cmd = [samloader, "check-update", "-m", model.upper(), "-r", region.upper()]
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"check-update failed: {exc!r}")
        return None
    log(f"exit {proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-300:]}")
    out = proc.stdout.strip()
    return out.splitlines()[-1].strip() if proc.returncode == 0 and out else None


def download_firmware(samloader: str | None, settings: dict, firmware_dir: Path, model: str, region: str,
                      echo=print) -> Path | None:
    """Download the latest official firmware. Returns the zip path, or None on failure."""
    if not samloader or not settings.get("firmware_download_enabled", True) or not model:
        return None
    chosen = None
    for r in regions_to_try(region):
        version = check_update(samloader, model, r)
        if version:
            chosen = r
            if r != region.upper():
                echo(f"Samsung publishes this model's firmware under region {r} (includes {region or 'your region'}) - using that.")
            echo(f"Latest firmware: {version}")
            break
    if not chosen:
        echo(f"Samsung's servers have no firmware for {model} in regions {', '.join(regions_to_try(region))}.")
        return None
    out_dir = firmware_dir / model.upper() / chosen
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = download_command(samloader, model, chosen, out_dir, int(settings.get("download_threads", 8)))
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
