# Galaxy Refurb

Turn ex-store / display Samsung Galaxy phones into clean, sellable phones on a Windows PC.

For each phone it walks you through three steps and records the result in a spreadsheet:

1. **Triage** — reads the model, serial, region, firmware, store/Knox management, signed-in accounts and demo (LDU) firmware. You enter the IMEI and check it isn't blacklisted.
2. **Flash + wipe** — downloads the official Samsung firmware for that model straight from Samsung's update servers and installs it. This removes the store software and factory-resets the phone.
3. **Final check** — confirms the phone isn't locked to the store, the store apps are gone, the IMEI is present, SIM/calls work and the battery is healthy, then has you do a final reset so the buyer gets a fresh "Welcome" screen.

> **What it will not do:** remove a Knox enrollment lock, a Google account lock (FRP) or a Samsung Reactivation Lock, or touch a blacklisted IMEI. Those phones are marked **RETURN_TO_SELLER**, with the IMEI recorded, so the seller can release them.

---

## Install (one command)

On the Windows 10/11 PC:

1. Press **Start**, type **PowerShell**, and open **Windows PowerShell** (no admin needed).
2. Paste this line and press Enter:

```powershell
irm https://raw.githubusercontent.com/Azclawd/galaxy-refurb/main/install.ps1 | iex
```

It takes about 2-5 minutes. It installs everything into `%LOCALAPPDATA%\GalaxyRefurb` and puts a **Galaxy Refurb** icon on your Desktop. To update later, run the same command again.

**Samsung USB driver.** If the installer says it's missing, it opens Samsung's page. Download *Samsung Android USB Driver for Windows*, run it and click Next until it finishes. You only need to do this once per PC.

What gets installed:

| Piece | What it's for | From |
|---|---|---|
| Python 3.12 (private copy) | runs this tool | python.org |
| adb (platform-tools) | reads the phone over USB | Google, official |
| samloader-rs 2.2.0 | downloads official firmware from Samsung and flashes it | [topjohnwu/samloader-rs](https://github.com/topjohnwu/samloader-rs) (open source, checksum verified) |

---

## Doing a phone

Double-click **Galaxy Refurb** and choose **1. Process a phone**, then follow the prompts on screen. For a typical phone:

1. **Turn on USB debugging** on the phone (the tool shows the steps), plug it in and tap **Allow**.
   - If a store phone won't let you do this, type **M** when asked. The tool reads the model from the phone's Download mode instead.
2. **Type the IMEI** (dial `*#06#`) and check it on CheckMEND, which the tool opens for you.
3. If Google or Samsung accounts are signed in, **remove them** when told. Otherwise the phone will lock after the reset.
4. **Confirm the wipe.** The firmware downloads (5-12 GB the first time for each model, then it's reused) and flashes automatically. Don't unplug the phone.
5. **Set up the phone** without signing in to anything, turn USB debugging back on, and answer a few yes/no checks.
6. On a **PASS**, do the final Factory data reset the tool shows you. The phone is then ready to box.

Every phone gets a row in **Documents\GalaxyRefurb\inventory.csv**, which opens in Excel. Menu option 4 opens it.

### Results you'll see

| Result | Meaning |
|---|---|
| `PASS_READY_TO_SELL` | Clean and working. Sell it. |
| `FAIL_NEEDS_ATTENTION` | Something's off (still demo firmware, no IMEI, hardware fault…). The notes column says what. |
| `LOCKED_KNOX_RETURN_TO_SELLER` | After the wipe it re-enrolled into the store's Knox account. Only the seller can release it. |
| `LOCKED_ACCOUNT_RETURN_TO_SELLER` | It asked for the previous Google/Samsung account. Only the account owner can remove it. |
| `RETURN_TO_SELLER` | Blacklisted IMEI, or accounts that couldn't be removed. |

---

## Good to know

- **Region code (CSC).** Firmware is downloaded for the phone's own region (read automatically, e.g. `BTU` = UK, `EUX` = Europe). If a download fails for a demo unit, try `BTU` or `EUX` when asked.
- **Demo units (LDU).** Many convert to normal phones with retail firmware. Some keep demo restrictions in hardware (no IMEI, no SIM). The final check catches this. Don't sell those as working phones.
- **Knox warranty bit.** If it's tripped, the phone works but Samsung Pay / Secure Folder won't. The tool flags it so you can mention it in the listing.
- **Disk space.** Allow about 25 GB per phone model (the zip plus an unpacked copy, kept so the next phone of the same model is instant). Delete folders in `Documents\GalaxyRefurb\firmware` when you've finished a model.
- **Selling.** List the phones honestly as ex-display / refurbished.

## If automatic flashing fails: Odin

The tool offers a guided **Odin** flash instead. It uses the same Samsung driver, so nothing needs changing. Samsung doesn't publish Odin for public download. If you want this fallback, get a recent Odin3 from a source you trust and put it in `%LOCALAPPDATA%\GalaxyRefurb\tools\odin\`, and the tool will open it for you. A phone that failed mid-flash is not broken: it stays in Download mode and can be flashed again.

## Adding firmware by hand

If the automatic download fails, put the official firmware zip (or the `BL_`, `AP_`, `CP_`, `CSC_` `.tar.md5` files) in:

```
Documents\GalaxyRefurb\firmware\SM-XXXXX\
```

Use the phone's model number as the folder name, then press Enter in the tool. Open-source tools that download official firmware: [samloader-rs](https://github.com/topjohnwu/samloader-rs) and [Bifrost](https://github.com/zacharee/Bifrost).

## Troubleshooting

| Problem | Fix |
|---|---|
| "No phone found" | Use a data cable (not charge-only), try another USB port, check the Samsung driver is installed, and tap **Allow** on the phone. |
| Shows `unauthorized` | Unlock the phone and tap **Allow**. Tick "Always allow". |
| Can't see the phone in Download mode | Different cable/port; reinstall the Samsung driver; unplug and replug. |
| Firmware download fails | Check the region code; try `EUX`/`BTU`; or add the firmware by hand (see above). |
| Anything else | Each phone has a log in `Documents\GalaxyRefurb\logs`. Send that file. |

Settings live in `%LOCALAPPDATA%\GalaxyRefurb\settings.json` (optional). For example, `{"flash_backend": "odin"}` always uses guided Odin.

## Credits

- [samloader-rs](https://github.com/topjohnwu/samloader-rs) by John Wu, Apache-2.0. It's derived from [Heimdall](https://git.sr.ht/~grimler/Heimdall).
- Android platform-tools by Google.

This project is MIT licensed. It's provided as-is: flashing firmware always carries some risk, so use it on phones you own.
