#Requires -RunAsAdministrator
<#
.SYNOPSIS
    SMART Sniffer Agent — Windows Installer

.DESCRIPTION
    Downloads and installs the SMART Sniffer Agent as a Windows service.

    One-liner install (PowerShell as Administrator):
      irm https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.ps1 | iex

    Or pin a specific version:
      $env:VERSION = "0.1.0"; irm ... | iex

.NOTES
    Requires: Administrator privileges, smartmontools for Windows
#>

$ErrorActionPreference = "Stop"

$Repo       = "DAB-LABS/smart-sniffer"
$BinaryName = "smartha-agent"
$InstallDir = "$env:ProgramFiles\SmartHA-Agent"
$BinaryPath = "$InstallDir\$BinaryName.exe"
$ConfigDir  = "$InstallDir"
$ConfigFile = "$ConfigDir\config.yaml"

# ServiceName must match the Go const in agent/service.go. The Go
# binary calls svc.Run(ServiceName, ...) and the SCM will only route
# control codes to us if the name registered here matches what the
# binary announces. Changing one side without the other will reproduce
# Error 1053 on service start.
$ServiceName = "SmartHA-Agent"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step  { param($msg) Write-Host "  --> $msg" -ForegroundColor White }
function Write-Ok    { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "  [X] $msg" -ForegroundColor Red; exit 1 }

# IsInteractive returns $true when we have a console attached and the
# user can respond to Read-Host prompts. irm ... | iex over a remote
# session leaves a TTY, but some automation contexts do not. Match
# install.sh semantics: fall back to "keep existing config" when we
# cannot prompt, so unattended upgrades don't lose user settings.
function Test-Interactive {
    try {
        return [Environment]::UserInteractive -and ($Host.UI.RawUI -ne $null)
    } catch {
        return $false
    }
}

# Read-YamlScalar pulls a single top-level scalar value out of a
# simple flat YAML file. Matches the install.sh approach of using
# grep+awk rather than introducing a YAML parser dependency. Works
# for port, token, scan_interval, advertise_interface, mdns_name;
# does NOT work for the nested filesystems: block (that requires
# multi-line parsing and is out of scope for Change 5 — see
# docs/internal/trackers/windows-installer-parity-backlog.md).
function Read-YamlScalar {
    param(
        [string]$Path,
        [string]$Key
    )
    if (-not (Test-Path $Path)) { return $null }
    $line = Get-Content -Path $Path | Where-Object { $_ -match "^$Key\s*:" } | Select-Object -First 1
    if (-not $line) { return $null }
    $value = ($line -split ':', 2)[1].Trim().Trim('"').Trim("'")
    if ($value -eq '') { return $null }
    return $value
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "    SMART Sniffer Agent - Windows Installer" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# Detect architecture
# ---------------------------------------------------------------------------
$Arch = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else {
    Write-Fail "32-bit Windows is not supported."
}
$BinaryFile = "$BinaryName-windows-$Arch.exe"
Write-Step "Detected platform: windows/$Arch"

# ---------------------------------------------------------------------------
# Resolve version
# ---------------------------------------------------------------------------
$Version = $env:VERSION
if (-not $Version) {
    Write-Step "Fetching latest release version..."
    try {
        $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing
        $Version = $Release.tag_name -replace '^v', ''
    } catch {
        Write-Fail "Could not determine latest version. Set `$env:VERSION = 'x.y.z'` manually."
    }
}
if (-not $Version) {
    Write-Fail "Could not determine latest version."
}
Write-Ok "Version: v$Version"

$ReleaseUrl   = "https://github.com/$Repo/releases/download/v$Version"
$BinaryUrl    = "$ReleaseUrl/$BinaryFile"
$ChecksumsUrl = "$ReleaseUrl/checksums.txt"

# ---------------------------------------------------------------------------
# Download binary and verify checksum
# ---------------------------------------------------------------------------
$TmpDir = Join-Path $env:TEMP "smartha-install-$(Get-Random)"
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null

try {
    Write-Step "Downloading $BinaryFile..."
    $BinaryTmp = Join-Path $TmpDir $BinaryFile
    try {
        Invoke-WebRequest -Uri $BinaryUrl -OutFile $BinaryTmp -UseBasicParsing
    } catch {
        Write-Fail "Download failed. Check that version v$Version exists at:`n  $BinaryUrl"
    }

    Write-Step "Verifying checksum..."
    $ChecksumsTmp = Join-Path $TmpDir "checksums.txt"
    $SkipChecksum = $false
    try {
        Invoke-WebRequest -Uri $ChecksumsUrl -OutFile $ChecksumsTmp -UseBasicParsing
    } catch {
        Write-Warn "Could not download checksums - skipping verification."
        $SkipChecksum = $true
    }

    if (-not $SkipChecksum -and (Test-Path $ChecksumsTmp)) {
        $ChecksumLine = Get-Content $ChecksumsTmp | Where-Object { $_ -match $BinaryFile }
        if ($ChecksumLine) {
            $Expected = ($ChecksumLine -split '\s+')[0]
            $Actual = (Get-FileHash -Path $BinaryTmp -Algorithm SHA256).Hash.ToLower()
            if ($Expected -eq $Actual) {
                Write-Ok "Checksum verified."
            } else {
                Write-Fail "Checksum mismatch!`n  Expected: $Expected`n  Got:      $Actual"
            }
        } else {
            Write-Warn "Binary not found in checksums file - skipping verification."
        }
    }

    # ---------------------------------------------------------------------------
    # Check for smartmontools
    # ---------------------------------------------------------------------------
    Write-Step "Checking for smartmontools..."
    $SmartCtl = Get-Command smartctl -ErrorAction SilentlyContinue
    if (-not $SmartCtl) {
        # Check common install locations
        $SmartCtlPaths = @(
            "$env:ProgramFiles\smartmontools\bin\smartctl.exe",
            "${env:ProgramFiles(x86)}\smartmontools\bin\smartctl.exe"
        )
        $Found = $SmartCtlPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($Found) {
            Write-Ok "smartctl found: $Found"
        } else {
            Write-Warn "smartctl not found."
            Write-Host ""
            Write-Host "  smartmontools is required. Install options:" -ForegroundColor Yellow
            Write-Host "    1. winget install smartmontools" -ForegroundColor Yellow
            Write-Host "    2. choco install smartmontools" -ForegroundColor Yellow
            Write-Host "    3. Download from https://www.smartmontools.org/wiki/Download" -ForegroundColor Yellow
            Write-Host ""

            if (Get-Command winget -ErrorAction SilentlyContinue) {
                $Install = Read-Host "  Install via winget now? [Y/n]"
                if ($Install -ne 'n' -and $Install -ne 'N') {
                    winget install smartmontools --accept-package-agreements --accept-source-agreements
                }
            } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
                $Install = Read-Host "  Install via Chocolatey now? [Y/n]"
                if ($Install -ne 'n' -and $Install -ne 'N') {
                    choco install smartmontools -y
                }
            } else {
                Write-Fail "Please install smartmontools manually and re-run this installer."
            }
        }
    } else {
        Write-Ok "smartctl found: $($SmartCtl.Source)"
    }

    # ---------------------------------------------------------------------------
    # Upgrade detection — mirror install.sh's config-preservation UX
    # ---------------------------------------------------------------------------
    #
    # If a config.yaml already exists, parse the known scalar fields
    # and offer to keep them. Matches install.sh's behavior so Windows
    # users on an upgrade path do not silently lose their bearer token
    # or custom port. See Change 5 in
    # docs/internal/plans/plan-v0.5.1-consolidated-changes.md.
    $KeepConfig       = $false
    $ExistingPort     = $null
    $ExistingToken    = $null
    $ExistingInterval = $null
    $ExistingIface    = $null

    if (Test-Path $ConfigFile) {
        Write-Step "Existing configuration detected at $ConfigFile"
        $ExistingPort     = Read-YamlScalar -Path $ConfigFile -Key 'port'
        $ExistingToken    = Read-YamlScalar -Path $ConfigFile -Key 'token'
        $ExistingInterval = Read-YamlScalar -Path $ConfigFile -Key 'scan_interval'
        $ExistingIface    = Read-YamlScalar -Path $ConfigFile -Key 'advertise_interface'

        Write-Host ""
        Write-Host "  Current settings:" -ForegroundColor White
        if ($ExistingPort)     { Write-Host "    Port              : $ExistingPort" }
        if ($ExistingInterval) { Write-Host "    Scan interval     : $ExistingInterval" }
        if ($ExistingToken)    { Write-Host "    Token             : (set)" } else { Write-Host "    Token             : (none)" }
        if ($ExistingIface)    { Write-Host "    Advertise interface: $ExistingIface" }
        Write-Host ""

        if (Test-Interactive) {
            $Answer = Read-Host "  Keep existing configuration? [Y/n]"
            if ($Answer -eq 'n' -or $Answer -eq 'N') {
                $KeepConfig = $false
            } else {
                $KeepConfig = $true
            }
        } else {
            Write-Step "Non-interactive mode - keeping existing configuration."
            $KeepConfig = $true
        }
    }

    # ---------------------------------------------------------------------------
    # Configuration prompts (skipped when keeping existing)
    # ---------------------------------------------------------------------------
    if ($KeepConfig) {
        Write-Ok "Keeping existing configuration."
        $Port         = $ExistingPort
        $Token        = $ExistingToken
        $ScanInterval = $ExistingInterval
    } else {
        Write-Host ""
        Write-Host "  Configuration" -ForegroundColor White
        Write-Host "  (Press Enter to accept defaults)"
        Write-Host ""

        $Port = Read-Host "  Port [9099]"
        if (-not $Port) { $Port = "9099" }

        $Token = Read-Host "  Bearer token for API auth (leave blank to disable)"

        $ScanInterval = Read-Host "  Scan interval [60s]"
        if (-not $ScanInterval) { $ScanInterval = "60s" }
    }

    # ---------------------------------------------------------------------------
    # Install binary
    # ---------------------------------------------------------------------------
    Write-Host ""
    Write-Step "Installing binary to $BinaryPath..."
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

    # Stop existing service if running so the binary file lock releases.
    $ExistingSvc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($ExistingSvc -and $ExistingSvc.Status -eq 'Running') {
        Write-Warn "Stopping existing service..."
        Stop-Service -Name $ServiceName -Force
        Start-Sleep -Seconds 2
    }

    Copy-Item -Path $BinaryTmp -Destination $BinaryPath -Force
    Write-Ok "Binary installed."

    # ---------------------------------------------------------------------------
    # Write config (skipped when keeping existing)
    # ---------------------------------------------------------------------------
    if ($KeepConfig) {
        Write-Step "Keeping config at $ConfigFile"
    } else {
        Write-Step "Writing config to $ConfigFile..."
        $ConfigContent = "port: $Port`nscan_interval: $ScanInterval"
        if ($Token) {
            $ConfigContent += "`ntoken: `"$Token`""
        }
        Set-Content -Path $ConfigFile -Value $ConfigContent -Encoding UTF8
        Write-Ok "Config written."
    }

    # ---------------------------------------------------------------------------
    # Register Event Log source
    # ---------------------------------------------------------------------------
    #
    # Change 4 made the Go binary write startup/shutdown/error events to
    # the Application log under source $ServiceName. The source has to
    # be registered once (Windows requires SYSTEM or Administrator to
    # create it) or the binary falls back to stderr, which is invisible
    # under the SCM. We guard on SourceExists so re-running this script
    # on an upgrade does not throw.
    Write-Step "Registering Event Log source..."
    if ([System.Diagnostics.EventLog]::SourceExists($ServiceName)) {
        Write-Ok "Event Log source already registered."
    } else {
        try {
            New-EventLog -LogName Application -Source $ServiceName -ErrorAction Stop
            Write-Ok "Event Log source registered."
        } catch {
            Write-Warn "Could not register Event Log source: $($_.Exception.Message)"
            Write-Warn "Service will still run; error detail will go to stderr instead of Event Viewer."
        }
    }

    # ---------------------------------------------------------------------------
    # Install Windows service
    # ---------------------------------------------------------------------------
    Write-Step "Installing Windows service..."

    # Remove existing service if present. sc.exe delete is intentional
    # here instead of Remove-Service (which requires PS 6+); we check
    # $LASTEXITCODE so a stuck "marked for deletion" state surfaces
    # loudly instead of the installer happily continuing to New-Service
    # and reporting a confusing "service already exists" later.
    if ($ExistingSvc) {
        Write-Warn "Removing existing service..."
        sc.exe delete $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "sc.exe delete $ServiceName failed with exit code $LASTEXITCODE. The service may be marked for deletion; reboot and re-run the installer."
        }
        Start-Sleep -Seconds 2
    }

    # Create the service with the full command line in one call so
    # there is a single source of truth for the ImagePath. Previous
    # versions called New-Service with just the binary, then patched
    # ImagePath via the registry to add --config; that left room for
    # the two to drift if one was ever changed without the other.
    $SvcBinPath = "`"$BinaryPath`" --config `"$ConfigFile`""
    New-Service -Name $ServiceName `
        -DisplayName "SMART Sniffer Agent" `
        -Description "SMART Sniffer Agent - disk health REST API for Home Assistant" `
        -BinaryPathName $SvcBinPath `
        -StartupType Automatic `
        -ErrorAction Stop | Out-Null

    # Set recovery: restart on first, second, and subsequent failures
    sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/10000 | Out-Null

    # Start the service with proper error surfacing. On failure we dump
    # recent Event Log entries from our source plus current service
    # status so the user has the context in one place without needing
    # to hunt through Event Viewer.
    Write-Step "Starting service..."
    try {
        Start-Service -Name $ServiceName -ErrorAction Stop
        Write-Ok "Windows service installed and started."
    } catch {
        Write-Host ""
        Write-Warn "Service failed to start: $($_.Exception.Message)"
        Write-Host ""
        Write-Host "  Service status:" -ForegroundColor Yellow
        try {
            Get-Service -Name $ServiceName | Format-List Name, Status, StartType, DisplayName | Out-String | Write-Host
        } catch { }
        Write-Host "  Recent Event Log entries for $ServiceName :" -ForegroundColor Yellow
        try {
            Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName=$ServiceName} -MaxEvents 10 -ErrorAction Stop `
                | Format-Table TimeCreated, Id, LevelDisplayName, Message -AutoSize -Wrap `
                | Out-String | Write-Host
        } catch {
            Write-Host "    (no events yet — source may have just been registered)" -ForegroundColor Yellow
        }
        Write-Fail "Start-Service failed. See diagnostics above."
    }

    # ---------------------------------------------------------------------------
    # Success banner
    # ---------------------------------------------------------------------------
    Write-Host ""
    Write-Host "  ================================================" -ForegroundColor Green
    Write-Host "    SMART Sniffer Agent installed successfully!" -ForegroundColor Green
    Write-Host "  ================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Endpoint : http://localhost:$Port/api/health"
    Write-Host "  Config   : $ConfigFile"
    Write-Host ""
    Write-Host "  Commands:"
    Write-Host "    Status:  Get-Service $ServiceName"
    Write-Host "    Logs:    Get-WinEvent -LogName Application -FilterXPath '*[System[Provider[@Name=""$ServiceName""]]]'"
    Write-Host "    Stop:    Stop-Service $ServiceName"
    Write-Host "    Start:   Start-Service $ServiceName"
    Write-Host "    Restart: Restart-Service $ServiceName"
    Write-Host ""

    # ---------------------------------------------------------------------------
    # Health check
    # ---------------------------------------------------------------------------
    #
    # Extended to 30 seconds (15 x 2s) to match the Go binary's 20s
    # ready-watchdog plus margin. On boxes with many disks the preflight
    # smartctl --scan can burn most of that window before the HTTP
    # listener binds, so the previous 10s cap would flag false negatives.
    Write-Step "Waiting for agent to start..."
    $Healthy = $false
    for ($i = 1; $i -le 15; $i++) {
        Start-Sleep -Seconds 2
        try {
            $null = Invoke-WebRequest -Uri "http://localhost:$Port/api/health" -UseBasicParsing -TimeoutSec 3
            Write-Ok "Health check passed - agent is running!"
            $Healthy = $true
            break
        } catch {
            # Keep trying
        }
    }
    if (-not $Healthy) {
        Write-Warn "Health check didn't respond after 30s."
        Write-Warn "Check service status: Get-Service $ServiceName"
        Write-Warn "Check Event Log    : Get-WinEvent -LogName Application -FilterXPath '*[System[Provider[@Name=""$ServiceName""]]]' -MaxEvents 20"
    }
    Write-Host ""

} finally {
    # Cleanup temp directory
    if (Test-Path $TmpDir) {
        Remove-Item -Path $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
