#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Removes the smartha-agent Windows service and optionally deletes install files.

.EXAMPLE
    .\uninstall-service.ps1
    .\uninstall-service.ps1 -RemoveFiles
#>

param(
    [switch]$RemoveFiles,
    [string]$InstallDir = "C:\Program Files\smartha-agent"
)

$ServiceName = "SmarthaAgent"

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Write-Host "Service '$ServiceName' is not installed. Nothing to do."
    exit 0
}

Write-Host "Stopping service..."
Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue

Write-Host "Removing service..."
sc.exe delete $ServiceName | Out-Null

if ($RemoveFiles -and (Test-Path $InstallDir)) {
    Write-Host "Removing install directory: $InstallDir"
    Remove-Item -Recurse -Force $InstallDir
}

Write-Host "smartha-agent service removed."
