#Requires -RunAsAdministrator
<#
.SYNOPSIS
    SMART Sniffer Agent -- Windows Installer

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
# multi-line parsing and is out of scope for Change 5 -- see
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
# Pick-Interface -- enumerate network adapters, present numbered list,
# return the selected FriendlyName or "" for auto-filter.
# Matches install.sh pick_interface() UX.
# ---------------------------------------------------------------------------
function Pick-Interface {
    # Enumerate all adapters with IPv4 addresses.
    $AllAdapters = @()
    try {
        $AllAdapters = @(Get-NetAdapter -ErrorAction SilentlyContinue |
            Where-Object { $_.Status -eq 'Up' })
    } catch {
        return ""
    }

    # Virtual adapter patterns (matched against InterfaceDescription).
    $VirtualPatterns = @(
        '*Hyper-V*', '*vEthernet*',
        '*TAP-Windows*', '*TAP*',
        '*Tailscale*',
        '*WireGuard*',
        '*WSL*',
        '*Loopback*'
    )

    # Build adapter list with IPs and virtual classification.
    $AdapterList = @()
    foreach ($Adapter in $AllAdapters) {
        $IPv4 = @(Get-NetIPAddress -InterfaceIndex $Adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -ne '127.0.0.1' })
        if ($IPv4.Count -eq 0) { continue }

        $IP = $IPv4[0].IPAddress
        $IsVirtual = $false
        $VirtualTag = ""
        foreach ($Pattern in $VirtualPatterns) {
            if ($Adapter.InterfaceDescription -like $Pattern -or $Adapter.Name -like $Pattern) {
                $IsVirtual = $true
                # Determine tag label.
                if ($Pattern -match 'Hyper-V|vEthernet') { $VirtualTag = "(Hyper-V)" }
                elseif ($Pattern -match 'TAP')            { $VirtualTag = "(TAP/VPN)" }
                elseif ($Pattern -match 'Tailscale')      { $VirtualTag = "(Tailscale)" }
                elseif ($Pattern -match 'WireGuard')      { $VirtualTag = "(WireGuard)" }
                elseif ($Pattern -match 'WSL')            { $VirtualTag = "(WSL)" }
                elseif ($Pattern -match 'Loopback')       { $VirtualTag = "(Loopback)" }
                else                                      { $VirtualTag = "(virtual)" }
                break
            }
        }

        $AdapterList += [PSCustomObject]@{
            Name       = $Adapter.Name
            IP         = $IP
            IsVirtual  = $IsVirtual
            VirtualTag = $VirtualTag
        }
    }

    if ($AdapterList.Count -eq 0) {
        Write-Step "No network adapters with IPv4 addresses detected."
        return ""
    }

    # Count non-virtual adapters with IPs.
    $NonVirtualCount = @($AdapterList | Where-Object { -not $_.IsVirtual }).Count

    # Auto-select if only one non-virtual adapter.
    if ($NonVirtualCount -le 1) {
        $SingleAdapter = $AdapterList | Where-Object { -not $_.IsVirtual } | Select-Object -First 1
        if ($SingleAdapter) {
            Write-Ok "Network interface: $($SingleAdapter.Name) ($($SingleAdapter.IP))"
        } else {
            Write-Ok "Network interface: auto"
        }
        return ""
    }

    # Non-interactive: skip picker.
    if (-not (Test-Interactive)) {
        return ""
    }

    # Present the picker.
    Write-Host ""
    Write-Host "  Network Interface (mDNS)" -ForegroundColor White
    Write-Host "  Home Assistant uses this to auto-discover the agent."
    Write-Host ""

    for ($i = 0; $i -lt $AdapterList.Count; $i++) {
        $Entry = $AdapterList[$i]
        $Label = "    $($i + 1)) $($Entry.Name)"
        $Label = $Label.PadRight(30) + $Entry.IP
        if ($Entry.IsVirtual) {
            Write-Host "$Label  " -NoNewline
            Write-Host $Entry.VirtualTag -ForegroundColor Yellow
        } else {
            Write-Host $Label
        }
    }

    Write-Host ""
    $Range = "1-$($AdapterList.Count)"
    $Choice = Read-Host "  Advertise on ($Range / all) [all]"
    if (-not $Choice) { $Choice = "all" }

    switch -Regex ($Choice.Trim().ToLower()) {
        '^(all|a)$' {
            Write-Step "mDNS: auto-filter mode (all physical interfaces)."
            return ""
        }
        '^\d+$' {
            $Num = [int]$Choice
            if ($Num -ge 1 -and $Num -le $AdapterList.Count) {
                $Selected = $AdapterList[$Num - 1].Name
                Write-Ok "mDNS will advertise on: $Selected"
                return $Selected
            } else {
                Write-Warn "Invalid choice -- using auto-filter."
                return ""
            }
        }
        default {
            Write-Warn "Invalid choice -- using auto-filter."
            return ""
        }
    }
}

# ---------------------------------------------------------------------------
# Pick-Filesystem -- enumerate fixed disk volumes, present numbered list,
# return YAML block string or "" if none selected.
# Matches install.sh pick_filesystems() UX.
# ---------------------------------------------------------------------------
function Pick-Filesystem {
    $Volumes = @()
    try {
        $Volumes = @(Get-Volume | Where-Object {
            $_.DriveLetter -and
            $_.FileSystemType -ne 'Unknown' -and
            $_.DriveType -eq 'Fixed'
        } | Sort-Object DriveLetter)
    } catch {
        Write-Step "Could not enumerate disk volumes."
        return ""
    }

    if ($Volumes.Count -eq 0) {
        Write-Step "No fixed disk volumes detected -- skipping disk usage monitoring."
        return ""
    }

    # Non-interactive: skip picker.
    if (-not (Test-Interactive)) {
        return ""
    }

    # Build display list.
    $VolumeList = @()
    foreach ($Vol in $Volumes) {
        $Letter = "$($Vol.DriveLetter):\"
        $FSType = $Vol.FileSystemType
        $TotalBytes = $Vol.Size
        $FreeBytes  = $Vol.SizeRemaining

        # Human-readable size.
        if ($TotalBytes -gt 1099511627776) {
            $HRSize = "{0:F1}T" -f ($TotalBytes / 1099511627776)
        } elseif ($TotalBytes -gt 1073741824) {
            $HRSize = "{0:F0}G" -f ($TotalBytes / 1073741824)
        } elseif ($TotalBytes -gt 1048576) {
            $HRSize = "{0:F0}M" -f ($TotalBytes / 1048576)
        } else {
            $HRSize = "${TotalBytes}B"
        }

        # Percent used.
        $PctUsed = 0
        if ($TotalBytes -gt 0) {
            $PctUsed = [math]::Round(($TotalBytes - $FreeBytes) / $TotalBytes * 100)
        }

        # Extract UUID (GUID) from UniqueId.
        $UUID = ""
        if ($Vol.UniqueId -match '\{(.+?)\}') {
            $UUID = $Matches[1]
        }

        $VolumeList += [PSCustomObject]@{
            Letter  = $Letter
            FSType  = $FSType
            HRSize  = $HRSize
            PctUsed = $PctUsed
            UUID    = $UUID
            Path    = $Letter
        }
    }

    # Present the picker.
    Write-Host ""
    Write-Host "  Disk Usage Monitoring" -ForegroundColor White
    Write-Host "  Select drives to report to Home Assistant."
    Write-Host ""

    for ($i = 0; $i -lt $VolumeList.Count; $i++) {
        $Entry = $VolumeList[$i]
        $Label = "    $($i + 1)) $($Entry.Letter)".PadRight(14)
        $Label += "$($Entry.FSType)".PadRight(10)
        $Label += "$($Entry.HRSize)".PadRight(8)
        $Label += "($($Entry.PctUsed)% used)"
        Write-Host $Label
    }

    Write-Host ""
    $RangeHint = "1"
    if ($VolumeList.Count -gt 1) { $RangeHint = "1,$( 2 )..$($VolumeList.Count)" }
    $Choice = Read-Host "  Monitor ($RangeHint / all / none) [all]"
    if (-not $Choice) { $Choice = "all" }

    $SelectedIndices = @()
    switch -Regex ($Choice.Trim().ToLower()) {
        '^(all|a)$' {
            $SelectedIndices = 0..($VolumeList.Count - 1)
            break
        }
        '^(none|n)$' {
            Write-Step "Disk usage monitoring disabled."
            return ""
        }
        default {
            # Comma-separated numbers.
            $Nums = $Choice -split ',' | ForEach-Object { $_.Trim() }
            foreach ($Num in $Nums) {
                if ($Num -match '^\d+$') {
                    $N = [int]$Num
                    if ($N -ge 1 -and $N -le $VolumeList.Count) {
                        $SelectedIndices += ($N - 1)
                    } else {
                        Write-Warn "Skipping invalid choice: $Num"
                    }
                } else {
                    Write-Warn "Skipping invalid choice: $Num"
                }
            }
        }
    }

    if ($SelectedIndices.Count -eq 0) {
        Write-Step "No valid drives selected -- disk usage monitoring disabled."
        return ""
    }

    # Build YAML block.
    $YAML = "filesystems:"
    $DisplayPaths = @()
    foreach ($Idx in $SelectedIndices) {
        $Entry = $VolumeList[$Idx]
        # Escape backslash for YAML: C:\ becomes C:\\
        $YAMLPath = $Entry.Path -replace '\\', '\\'
        $YAML += "`n  - path: `"$YAMLPath`""
        $YAML += "`n    uuid: `"$($Entry.UUID)`""
        $YAML += "`n    device: `"`""
        $YAML += "`n    fstype: `"$($Entry.FSType)`""
        $DisplayPaths += $Entry.Path
    }

    $DisplayStr = $DisplayPaths -join ', '
    Write-Ok "Monitoring $($SelectedIndices.Count) drive(s): $DisplayStr"
    return $YAML
}

# Ensure-SmartmontoolsInPath adds the smartmontools bin directory to
# the system PATH if it exists on disk but is not already in PATH.
# This is critical because the agent runs as a Windows service under
# the SYSTEM account, which has a separate PATH from the current user.
# Without this, winget/choco installs smartmontools but the service
# cannot find smartctl.
#
# Deduplicates: if the directory appears multiple times (from repeated
# installs), it is collapsed to a single entry.
#
# After modifying the registry, broadcasts WM_SETTINGCHANGE so the
# SCM and all running processes pick up the new PATH immediately
# without requiring a reboot.
function Ensure-SmartmontoolsInPath {
    $SmartDirs = @(
        "$env:ProgramFiles\smartmontools\bin",
        "${env:ProgramFiles(x86)}\smartmontools\bin"
    )
    $SmartDir = $SmartDirs | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $SmartDir) { return }

    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $Entries = $MachinePath -split ';' | Where-Object { $_.Trim() -ne '' }

    # Check if already present (case-insensitive, ignore trailing backslash).
    $Normalized = $SmartDir.TrimEnd('\').ToLower()
    $AlreadyPresent = $Entries | Where-Object { $_.TrimEnd('\').ToLower() -eq $Normalized }

    if ($AlreadyPresent.Count -eq 1) {
        # Exactly one entry -- nothing to do.
        return
    }

    # Either missing or duplicated. Remove all existing entries for this
    # directory, then append exactly one.
    $Cleaned = $Entries | Where-Object { $_.TrimEnd('\').ToLower() -ne $Normalized }
    $NewPath = ($Cleaned + $SmartDir) -join ';'
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "Machine")

    if ($AlreadyPresent.Count -gt 1) {
        Write-Step "Deduplicated smartmontools PATH entries ($($AlreadyPresent.Count) -> 1)."
    } else {
        Write-Step "Added $SmartDir to system PATH."
    }

    # Broadcast WM_SETTINGCHANGE so the SCM picks up the new PATH
    # without requiring a reboot. This is what proper installers
    # (MSI, WiX, NSIS) do after modifying environment variables.
    try {
        Add-Type -Namespace Win32 -Name NativeMethods -MemberDefinition @"
            [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
            public static extern IntPtr SendMessageTimeout(
                IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam,
                uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
"@ -ErrorAction SilentlyContinue
        $HWND_BROADCAST = [IntPtr]0xFFFF
        $WM_SETTINGCHANGE = 0x1A
        $SMTO_ABORTIFHUNG = 0x0002
        $result = [UIntPtr]::Zero
        [Win32.NativeMethods]::SendMessageTimeout(
            $HWND_BROADCAST, $WM_SETTINGCHANGE,
            [UIntPtr]::Zero, "Environment",
            $SMTO_ABORTIFHUNG, 5000, [ref]$result) | Out-Null
        Write-Ok "Environment change broadcast sent."
    } catch {
        Write-Warn "Could not broadcast environment change. A reboot may be needed for the service to find smartctl."
    }
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

    # Capture the hash of the downloaded binary for post-copy verification.
    $SourceHash = (Get-FileHash -Path $BinaryTmp -Algorithm SHA256).Hash

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
                    winget install smartmontools --accept-package-agreements --accept-source-agreements --disable-interactivity --source winget
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
    # Ensure smartmontools is in the system PATH
    # ---------------------------------------------------------------------------
    #
    # winget and choco install smartmontools to Program Files but do not
    # always add the bin directory to the machine PATH. The agent runs
    # under the SYSTEM account whose PATH is separate from the current
    # user's; if the directory is missing, the service fails at startup
    # with "smartctl not found in PATH" (event ID 100). Fix this for
    # both fresh installs (where smartmontools was just installed above)
    # and upgrades (where smartmontools was installed previously but PATH
    # was never set).
    Ensure-SmartmontoolsInPath

    # ---------------------------------------------------------------------------
    # Upgrade detection -- mirror install.sh's config-preservation UX
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

        # --- Upgrade path: offer interface picker if field is missing ---
        if (-not $ExistingIface -and (Test-Interactive)) {
            # Count non-virtual adapters to decide whether to prompt.
            $UpgradeAdapters = @()
            try {
                $UpgradeAdapters = @(Get-NetAdapter -ErrorAction SilentlyContinue |
                    Where-Object { $_.Status -eq 'Up' })
            } catch { }

            $VirtualPatterns = @(
                '*Hyper-V*', '*vEthernet*', '*TAP-Windows*', '*TAP*',
                '*Tailscale*', '*WireGuard*', '*WSL*', '*Loopback*'
            )
            $UpgradeNonVirtual = 0
            foreach ($Adp in $UpgradeAdapters) {
                $HasIP = @(Get-NetIPAddress -InterfaceIndex $Adp.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                    Where-Object { $_.IPAddress -ne '127.0.0.1' })
                if ($HasIP.Count -eq 0) { continue }
                $IsVirt = $false
                foreach ($P in $VirtualPatterns) {
                    if ($Adp.InterfaceDescription -like $P -or $Adp.Name -like $P) {
                        $IsVirt = $true; break
                    }
                }
                if (-not $IsVirt) { $UpgradeNonVirtual++ }
            }

            if ($UpgradeNonVirtual -gt 1) {
                Write-Host ""
                Write-Warn "Your config doesn't specify a network interface for mDNS."
                Write-Host "  Machines with multiple interfaces may advertise on the wrong IP."

                $MigIface = Pick-Interface
                if ($MigIface) {
                    Add-Content -Path $ConfigFile -Value "advertise_interface: $MigIface" -Encoding UTF8
                    Write-Ok "Interface saved to existing config."
                }
            }
        }

        # --- Upgrade path: offer filesystem picker if field is missing ---
        $HasFilesystems = $false
        if (Test-Path $ConfigFile) {
            $HasFilesystems = (Get-Content -Path $ConfigFile | Where-Object { $_ -match '^filesystems:' }).Count -gt 0
        }
        if (-not $HasFilesystems -and (Test-Interactive)) {
            Write-Host ""
            Write-Step "Disk usage monitoring is now available."
            $MigFS = Pick-Filesystem
            if ($MigFS) {
                Add-Content -Path $ConfigFile -Value "" -Encoding UTF8
                Add-Content -Path $ConfigFile -Value $MigFS -Encoding UTF8
                Write-Ok "Filesystem monitoring added to existing config."
            }
        }
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

        # --- Fresh install: interface picker ---
        $PickedIface = Pick-Interface

        # --- Fresh install: filesystem picker ---
        $PickedFS = Pick-Filesystem
    }

    # ---------------------------------------------------------------------------
    # Install binary
    # ---------------------------------------------------------------------------
    Write-Host ""
    Write-Step "Installing binary to $BinaryPath..."
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

    # Stop existing service if running so the binary file lock releases.
    # Also stop services in a failed state -- they may still hold a
    # handle on the .exe. We use sc.exe stop which handles both cases
    # gracefully (returns 1062 "not started" on an already-stopped
    # service, which we ignore).
    $ExistingSvc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($ExistingSvc) {
        if ($ExistingSvc.Status -eq 'Running') {
            Write-Warn "Stopping existing service..."
            Stop-Service -Name $ServiceName -Force
        } else {
            # Force-stop via sc.exe to release any lingering handles
            # from a service in a failed/stopping state.
            sc.exe stop $ServiceName 2>$null | Out-Null
        }
        # Give the SCM time to fully release the binary file handle.
        Start-Sleep -Seconds 3
    }

    # Copy with retry. On Windows the SCM can briefly hold a file
    # handle on the binary after the service process has exited. If
    # the first copy produces a locked-file error or silently fails
    # to overwrite, we retry after a short wait.
    $CopyVerified = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Copy-Item -Path $BinaryTmp -Destination $BinaryPath -Force -ErrorAction SilentlyContinue
        if (Test-Path $BinaryPath) {
            $DestHash = (Get-FileHash -Path $BinaryPath -Algorithm SHA256).Hash
            if ($DestHash -eq $SourceHash) {
                $CopyVerified = $true
                break
            }
        }
        Write-Warn "Binary copy attempt $attempt - file hash mismatch, retrying..."
        Start-Sleep -Seconds 3
    }
    if (-not $CopyVerified) {
        Write-Fail "Could not overwrite the existing binary after 3 attempts. The old service may still hold a file lock. Reboot and re-run the installer."
    }
    Write-Ok "Binary installed and verified."

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
        if ($PickedIface) {
            $ConfigContent += "`nadvertise_interface: $PickedIface"
        }
        if ($PickedFS) {
            $ConfigContent += "`n$PickedFS"
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
            Write-Host "    (no events yet -- source may have just been registered)" -ForegroundColor Yellow
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
