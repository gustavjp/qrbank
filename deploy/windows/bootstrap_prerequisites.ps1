#Requires -Version 5.1
<#
.SYNOPSIS
    Prepare a Windows Server 2025 VPS for a LOCAL-ONLY QRBank preview.
.DESCRIPTION
    Without -Install, performs read-only checks.
    With -Install, asks for confirmation and uses WinGet to install missing
    Python 3.13, Git for Windows, and PostgreSQL 16 prerequisites.

    This script NEVER changes RDP, network/firewall rules, opens application
    ports, stores credentials, creates a database, or deploys QRBank.
    PostgreSQL installation is INTERACTIVE so its administrator password can
    be chosen privately in the vendor's installer.

    Review the file before running it on your VPS. Do not use real student data
    until the later HTTPS/SMTP/backup milestones are complete.
.EXAMPLE
    .\bootstrap_prerequisites.ps1
.EXAMPLE
    .\bootstrap_prerequisites.ps1 -Install
#>
[CmdletBinding()]
param([switch]$Install)

$ErrorActionPreference = 'Stop'

function Test-CommandPresent {
    param([string]$Name)
    return $null -ne (Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Test-Python313 {
    if (-not (Test-CommandPresent 'py')) { return $false }
    try {
        $result = & py -3.13 --version 2>&1
        return ($LASTEXITCODE -eq 0 -and "$result" -match '^Python 3\.13\.')
    } catch {
        return $false
    }
}

function Test-Git {
    if (Test-CommandPresent 'git') { return $true }
    return (Test-Path 'C:\Program Files\Git\cmd\git.exe')
}

function Get-PostgresServices {
    return @(Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue)
}

Write-Host 'QRBank Windows VPS prerequisite check' -ForegroundColor Cyan
Write-Host 'This script does not alter firewall or Remote Desktop settings.'

$isWindowsServer2025 = (Get-CimInstance Win32_OperatingSystem).Caption -match 'Windows Server 2025'
if (-not $isWindowsServer2025) {
    throw 'Expected Windows Server 2025. Stopping to avoid changing an unexpected computer.'
}

$firewallProfiles = @(Get-NetFirewallProfile)
Write-Host "`nWindows Defender Firewall:"
$firewallProfiles | Select-Object Name, Enabled | Format-Table -AutoSize

if (@($firewallProfiles | Where-Object { -not $_.Enabled }).Count -gt 0) {
    throw 'At least one firewall profile is disabled. Review Windows security before installing anything.'
}

$pythonReady = Test-Python313
$gitReady = Test-Git
$pgServices = @(Get-PostgresServices)
$pg16Ready = @($pgServices | Where-Object { $_.Name -like '*16*' }).Count -gt 0

Write-Host "Python 3.13 installed: $pythonReady"
Write-Host "Git installed: $gitReady"
Write-Host "PostgreSQL 16 service present: $pg16Ready"

if ($pgServices.Count -gt 0) {
    $pgServices | Select-Object Name, Status, StartType | Format-Table -AutoSize
}

# Read-only check: PostgreSQL must not be exposed on public interfaces.
$pgListeners = @(Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue)
if (@($pgListeners | Where-Object { $_.LocalAddress -in @('0.0.0.0', '::') }).Count -gt 0) {
    throw 'PostgreSQL port 5432 is listening on all interfaces. Restrict it to loopback before proceeding.'
}

if (-not $Install) {
    Write-Host "`nRead-only check complete. Run this file again with -Install to install missing software."
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as Administrator before using -Install.'
}

if (-not (Test-CommandPresent 'winget')) {
    throw 'WinGet is unavailable. Install/repair Windows App Installer using Microsoft documentation, then retry.'
}

if ($pgServices.Count -gt 0 -and -not $pg16Ready) {
    throw 'A different PostgreSQL service is present. Review it manually; this script will not install a second version.'
}

$pending = @()
if (-not $pythonReady) { $pending += @{ Label = 'Python 3.13'; Id = 'Python.Python.3.13' } }
if (-not $gitReady) { $pending += @{ Label = 'Git'; Id = 'Git.Git' } }
if (-not $pg16Ready) { $pending += @{ Label = 'PostgreSQL 16'; Id = 'PostgreSQL.PostgreSQL.16' } }

if ($pending.Count -eq 0) {
    Write-Host 'All prerequisites are already installed. No changes were made.' -ForegroundColor Green
    exit 0
}

Write-Host "`nPackages to install:"
foreach ($package in $pending) { Write-Host " - $($package.Label) ($($package.Id))" }
Write-Host 'PostgreSQL will ask you to choose its administrator password. Keep it private.'
Write-Host 'No RDP/firewall changes, database creation, or QRBank deployment will occur.'
$confirmation = Read-Host 'To continue, type INSTALL (anything else cancels)'
if ($confirmation -cne 'INSTALL') {
    Write-Host 'Cancelled. No software was installed.'
    exit 0
}

foreach ($package in $pending) {
    Write-Host "`nInstalling $($package.Label)..." -ForegroundColor Cyan
    & winget install --id $package.Id --exact --source winget --interactive --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet reported a failure installing $($package.Label) (exit code $LASTEXITCODE). Resolve it before retrying."
    }
}

Write-Host "`nInstallation commands completed." -ForegroundColor Green
Write-Host 'Close this PowerShell window, open a NEW one, and run:'
Write-Host '  py -3.13 --version'
Write-Host '  git --version'
Write-Host "  Get-Service -Name 'postgresql*'"
Write-Host 'Check PostgreSQL listens on 127.0.0.1 or ::1 ONLY.'
Write-Host 'Do not open inbound ports 5432 or 8000. QRBank has NOT been published.'
