#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs smartha-agent as a Windows service.

.DESCRIPTION
    Copies the smartha-agent binary and config to Program Files, then registers
    it as a Windows service that starts automatically at boot.
    Must be run from an elevated (Administrator) PowerShell prompt.

.EXAMPLE
    .\install-service.ps1
    .\install-service.ps1 -BinaryPath "C:\path\to\smartha-agent-windows-amd64.exe"
    .\install-service.ps1 -Port 9099 -Token "mysecrettoken"
#>

param(
    [string]$BinaryPath  = "$PSScriptRoot\..\build\smartha-agent-windows-amd64.exe",
    [string]$InstallDir  = "C:\Program Files\smartha-agent",
    [int]   $Port        = 9099,
    [string]$Token       = "",
    [string]$ScanInterval = "60s"
)

$ServiceName = "SmarthaAgent"
$DisplayName = "SMART Sniffer Agent"
$Description = "Exposes SMART disk health data over a REST API for Home Assistant."

# ---------------------------------------------------------------------------
# 1. Validate the binary exists
# ---------------------------------------------------------------------------
if (-not (Test-Path $BinaryPath)) {
    Write-Error "Binary not found at: $BinaryPath`nRun 'make windows-amd64' first."
    exit 1
}

# ---------------------------------------------------------------------------
# 2. Create install directory and copy files
# ---------------------------------------------------------------------------
Write-Host "Creating install directory: $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$DestBinary = Join-Path $InstallDir "smartha-agent.exe"
Write-Host "Copying binary to: $DestBinary"
Copy-Item -Force $BinaryPath $DestBinary

# ---------------------------------------------------------------------------
# 3. Write config.yaml
# ---------------------------------------------------------------------------
$ConfigPath = Join-Path $InstallDir "config.yaml"
Write-Host "Writing config: $ConfigPath"

$ConfigContent = @"
port: $Port
scan_interval: $ScanInterval
"@

# Only write the token field if one was provided.
if ($Token -ne "") {
    $ConfigContent += "`ntoken: `"$Token`""
}

Set-Content -Path $ConfigPath -Value $ConfigContent -Encoding UTF8

# ---------------------------------------------------------------------------
# 4. Remove existing service if present (clean reinstall)
# ---------------------------------------------------------------------------
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Stopping and removing existing service..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

# ---------------------------------------------------------------------------
# 5. Register the Windows service
#    The binary path includes the working directory flag so the agent finds
#    config.yaml next to the binary.
# ---------------------------------------------------------------------------
Write-Host "Registering Windows service: $ServiceName"

$BinPathWithArgs = "`"$DestBinary`""

New-Service `
    -Name        $ServiceName `
    -DisplayName $DisplayName `
    -Description $Description `
    -BinaryPathName $BinPathWithArgs `
    -StartupType Automatic | Out-Null

# Set the working directory via the registry so config.yaml is found.
$RegPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
Set-ItemProperty -Path $RegPath -Name "AppDirectory" -Value $InstallDir

# Configure service recovery: restart on failure after 10 seconds, up to 3 times.
sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/10000/restart/10000 | Out-Null

# ---------------------------------------------------------------------------
# 6. Start the service
# ---------------------------------------------------------------------------
Write-Host "Starting service..."
Start-Service -Name $ServiceName

$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "==========================================="
Write-Host " SMART Sniffer Agent installed successfully"
Write-Host "==========================================="
Write-Host " Service name : $ServiceName"
Write-Host " Status       : $($svc.Status)"
Write-Host " Install dir  : $InstallDir"
Write-Host " Config       : $ConfigPath"
Write-Host " Endpoint     : http://localhost:$Port/api/health"
Write-Host "==========================================="
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Start:   Start-Service $ServiceName"
Write-Host "  Stop:    Stop-Service $ServiceName"
Write-Host "  Status:  Get-Service $ServiceName"
Write-Host "  Logs:    Get-EventLog -LogName Application -Source $ServiceName -Newest 20"
