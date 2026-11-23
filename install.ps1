# Galaxy Refurb installer for Windows 10/11.
#
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/Azclawd/galaxy-refurb/main/install.ps1 | iex
#
# Installs everything into %LOCALAPPDATA%\GalaxyRefurb (no admin rights needed):
#   python\        private copy of Python (does not touch any other Python on the PC)
#   tools\platform-tools\   Google's official adb
#   tools\samloader\        samloader-rs (open-source Samsung firmware download + flash tool)
# and adds a "Galaxy Refurb" shortcut to the Desktop and Start menu.
# Re-running the same command updates everything.

function Install-GalaxyRefurb {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x faster without the progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $Repo          = 'Azclawd/galaxy-refurb'
    $Branch        = 'main'
    $PythonVersion = '3.12.8'
    $SamloaderUrl  = 'https://github.com/topjohnwu/samloader-rs/releases/download/2.2.0/samloader-v2.2.0-windows-x86_64.zip'
    $SamloaderSha  = '0132de1b79e208138f980c467f9a7eaa2ffcb72aae14afc3ed13799803c9b9ed'
    $AdbUrl        = 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip'
    $SamsungDriverPage = 'https://developer.samsung.com/android-usb-driver'

    $AppHome = Join-Path $env:LOCALAPPDATA 'GalaxyRefurb'
    $Tools   = Join-Path $AppHome 'tools'
    $PyDir   = Join-Path $AppHome 'python'
    $Py      = Join-Path $PyDir 'python.exe'
    $Tmp     = Join-Path $env:TEMP ('galaxy-refurb-' + [guid]::NewGuid().ToString('N'))

    function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
    function Good($m) { Write-Host "    OK  $m" -ForegroundColor Green }
    function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }
    function Get-File($url, $out) { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing }

    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Galaxy Refurb needs 64-bit Windows.' }
    New-Item -ItemType Directory -Force -Path $AppHome, $Tools, $Tmp | Out-Null

    try {
        # 1. Python (private copy)
        Say "Python $PythonVersion"
        if (-not (Test-Path $Py)) {
            $pyInstaller = Join-Path $Tmp 'python-installer.exe'
            Get-File "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" $pyInstaller
            $pyArgs = @('/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0', 'Include_test=0',
                      'Include_doc=0', 'Shortcuts=0', 'AssociateFiles=0', "TargetDir=$PyDir")
            $p = Start-Process -FilePath $pyInstaller -ArgumentList $pyArgs -Wait -PassThru
            if ($p.ExitCode -ne 0 -or -not (Test-Path $Py)) { throw "Python install failed (exit $($p.ExitCode))." }
        }
        Good (& $Py --version)

        # 2. Galaxy Refurb itself
        Say 'Galaxy Refurb app'
        & $Py -m pip install --quiet --disable-pip-version-check --upgrade pip | Out-Null
        & $Py -m pip install --quiet --disable-pip-version-check --no-cache-dir --upgrade --force-reinstall `
            "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
        if ($LASTEXITCODE -ne 0) { throw 'Installing the Galaxy Refurb app failed.' }
        Good ((& $Py -m galaxy_refurb --version) -join '')

        # 3. adb (Google platform-tools)
        Say 'Android platform-tools (adb)'
        $adbZip = Join-Path $Tmp 'platform-tools.zip'
        Get-File $AdbUrl $adbZip
        $oldAdb = Join-Path $Tools 'platform-tools\adb.exe'
        if (Test-Path $oldAdb) { try { & $oldAdb kill-server 2>$null | Out-Null } catch {} }
        if (Test-Path (Join-Path $Tools 'platform-tools')) { Remove-Item -Recurse -Force (Join-Path $Tools 'platform-tools') }
        Expand-Archive -Path $adbZip -DestinationPath $Tools -Force
        Good 'adb installed'

        # 4. samloader-rs (pinned version, checksum verified)
        Say 'samloader-rs (firmware download + flash)'
        $slZip = Join-Path $Tmp 'samloader.zip'
        Get-File $SamloaderUrl $slZip
        $hash = (Get-FileHash -Algorithm SHA256 $slZip).Hash.ToLower()
        if ($hash -ne $SamloaderSha) { throw "samloader download checksum mismatch ($hash). Not installing it." }
        $slTmp = Join-Path $Tmp 'samloader'
        Expand-Archive -Path $slZip -DestinationPath $slTmp -Force
        $exe = Get-ChildItem -Path $slTmp -Recurse -Filter 'samloader*.exe' | Select-Object -First 1
        if (-not $exe) { throw 'samloader.exe not found in download.' }
        $slDir = Join-Path $Tools 'samloader'
        New-Item -ItemType Directory -Force -Path $slDir | Out-Null
        Copy-Item -Force $exe.FullName (Join-Path $slDir 'samloader.exe')
        Good 'samloader installed (checksum verified)'

        # 5. Odin folder (optional fallback - you supply Odin yourself, see README)
        New-Item -ItemType Directory -Force -Path (Join-Path $Tools 'odin') | Out-Null

        # 6. Launcher + shortcuts
        Say 'Shortcuts'
        $launcher = Join-Path $AppHome 'GalaxyRefurb.cmd'
        $cmd = "@echo off`r`ntitle Galaxy Refurb (keep open while working)`r`n`"%~dp0python\python.exe`" -m galaxy_refurb %*`r`nif errorlevel 1 pause`r`n"
        [IO.File]::WriteAllText($launcher, $cmd, [Text.Encoding]::ASCII)
        $ws = New-Object -ComObject WScript.Shell
        $places = @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) ''))
        foreach ($dir in $places) {
            if (-not $dir) { continue }
            $sc = $ws.CreateShortcut((Join-Path $dir 'Galaxy Refurb.lnk'))
            $sc.TargetPath = $launcher
            $sc.WorkingDirectory = $AppHome
            $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,41"
            $sc.WindowStyle = 7   # start the helper console minimised; the app opens in its own window
            $sc.Save()
        }
        Good "Desktop + Start menu shortcut: 'Galaxy Refurb'"

        # 7. Samsung USB driver (needed for adb and flashing)
        Say 'Samsung USB driver'
        $drivers = (pnputil /enum-drivers) -join "`n"
        if ($drivers -match 'SAMSUNG Electronics') {
            Good 'Samsung USB driver already installed'
        } else {
            Warn 'Samsung USB driver not found. Opening Samsung''s download page:'
            Warn '  download "Samsung Android USB Driver for Windows", run it, click Next until done.'
            Start-Process $SamsungDriverPage
        }

        Write-Host ''
        Write-Host 'Galaxy Refurb is installed.' -ForegroundColor Green
        Write-Host "Your inventory, firmware and logs go to: $([Environment]::GetFolderPath('MyDocuments'))\GalaxyRefurb"
        Write-Host "Start it from the 'Galaxy Refurb' icon on your Desktop."
        Write-Host ''
        if (-not $env:GALAXY_REFURB_NO_START) {
            $go = Read-Host 'Start it now? [Y/n]'
            if ($go -eq '' -or $go -match '^[Yy]') { Start-Process $launcher }
        }
    }
    finally {
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    }
}

try { Install-GalaxyRefurb }
catch {
    Write-Host ''
    Write-Host "INSTALL FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Check your internet connection and run the same command again.' -ForegroundColor Red
    if ($env:GALAXY_REFURB_NO_START) { exit 1 }   # CI: fail the job
}
