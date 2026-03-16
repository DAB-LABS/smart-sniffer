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
$ServiceName = "SmartHA-Agent"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step  { param($msg) Write-Host "  --> $msg" -ForegroundColor White }
function Write-Ok    { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "  [X] $msg" -ForegroundColor Red; exit 1 }

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
    # Configuration
    # ---------------------------------------------------------------------------
    Write-Host ""
    Write-Host "  Configuration" -ForegroundColor White
    Write-Host "  (Press Enter to accept defaults)"
    Write-Host ""

    $Port = Read-Host "  Port [9099]"
    if (-not $Port) { $Port = "9099" }

    $Token = Read-Host "  Bearer token for API auth (leave blank to disable)"

    $ScanInterval = Read-Host "  Scan interval [60s]"
    if (-not $ScanInterval) { $ScanInterval = "60s" }

    # ---------------------------------------------------------------------------
    # Install binary
    # ---------------------------------------------------------------------------
    Write-Host ""
    Write-Step "Installing binary to $BinaryPath..."
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

    # Stop existing service if running
    $ExistingSvc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($ExistingSvc -and $ExistingSvc.Status -eq 'Running') {
        Write-Warn "Stopping existing service..."
        Stop-Service -Name $ServiceName -Force
        Start-Sleep -Seconds 2
    }

    Copy-Item -Path $BinaryTmp -Destination $BinaryPath -Force
    Write-Ok "Binary installed."

    # ---------------------------------------------------------------------------
    # Write config
    # ---------------------------------------------------------------------------
    Write-Step "Writing config to $ConfigFile..."
    $ConfigContent = "port: $Port`nscan_interval: $ScanInterval"
    if ($Token) {
        $ConfigContent += "`ntoken: `"$Token`""
    }
    Set-Content -Path $ConfigFile -Value $ConfigContent -Encoding UTF8
    Write-Ok "Config written."

    # ---------------------------------------------------------------------------
    # Install Windows service
    # ---------------------------------------------------------------------------
    Write-Step "Installing Windows service..."

    # Remove existing service if present
    if ($ExistingSvc) {
        Write-Warn "Removing existing service..."
        sc.exe delete $ServiceName | Out-Null
        Start-Sleep -Seconds 2
    }

    # Create the service
    $SvcBinPath = "`"$BinaryPath`""
    New-Service -Name $ServiceName `
        -DisplayName "SMART Sniffer Agent" `
        -Description "SMART Sniffer Agent - disk health REST API for Home Assistant" `
        -BinaryPathName $SvcBinPath `
        -StartupType Automatic `
        -ErrorAction Stop | Out-Null

    # Set recovery: restart on first, second, and subsequent failures
    sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/10000 | Out-Null

    # Set working directory via registry (service starts in InstallDir)
    # The agent reads config.yaml from its working directory
    $RegPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
    Set-ItemProperty -Path $RegPath -Name "ImagePath" -Value "`"$BinaryPath`" --config `"$ConfigFile`""

    Start-Service -Name $ServiceName
    Write-Ok "Windows service installed and started."

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
    Write-Step "Waiting for agent to start..."
    $Healthy = $false
    for ($i = 1; $i -le 5; $i++) {
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
        Write-Warn "Health check didn't respond after 10s."
        Write-Warn "Check service status: Get-Service $ServiceName"
    }
    Write-Host ""

} finally {
    # Cleanup temp directory
    if (Test-Path $TmpDir) {
        Remove-Item -Path $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
