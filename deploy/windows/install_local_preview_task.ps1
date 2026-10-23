#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers a local-only QRBank preview task on Windows Server.
.NOTES
    Preview only; not production-ready. Does not expose a public port.
    Store configuration outside the repository with Windows DPAPI and restrictive ACLs.
#>
[CmdletBinding()]
param(
    [string]$RepositoryPath = 'C:\Apps\qrbank'
)

$ErrorActionPreference = 'Stop'
$taskName = 'QRBankLocalPreview'
$repoRoot = (Resolve-Path -LiteralPath $RepositoryPath).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$runner = Join-Path $repoRoot 'deploy\windows\run_local_preview_task.ps1'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Missing .venv Python executable. Finish the Windows prerequisite setup first.'
}
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'Missing QRBank preview task runner.'
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Task $taskName already exists. This installer will not overwrite it."
}

$dataRoot = Join-Path $env:ProgramData 'QRBank'
$secretsDir = Join-Path $dataRoot 'secrets'
$logsDir = Join-Path $dataRoot 'logs'
foreach ($path in @($dataRoot, $secretsDir, $logsDir)) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
}

# Use stable Windows SIDs; translated group names vary with server locale.
$administrators = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$localService = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-19')

function Set-PrivateFolderAccess {
    param(
        [string]$Path,
        [System.Security.AccessControl.FileSystemRights]$ServiceRights
    )
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(
        @{ Sid = $administrators; Rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
        @{ Sid = $system; Rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
        @{ Sid = $localService; Rights = $ServiceRights }
    )) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $entry.Sid,
            $entry.Rights,
            [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

Set-PrivateFolderAccess -Path $dataRoot -ServiceRights ReadAndExecute
Set-PrivateFolderAccess -Path $secretsDir -ServiceRights ReadAndExecute
Set-PrivateFolderAccess -Path $logsDir -ServiceRights Modify

# A Local Service account needs read/execute access to the checked-out app.
# Do not grant it write access to the source code or virtual environment.
$repoAcl = Get-Acl -LiteralPath $repoRoot
$repoRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $localService,
    [System.Security.AccessControl.FileSystemRights]::ReadAndExecute,
    [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$repoAcl.AddAccessRule($repoRule)
Set-Acl -LiteralPath $repoRoot -AclObject $repoAcl

$secretFile = Join-Path $secretsDir 'database_url.bin'
if (Test-Path -LiteralPath $secretFile) {
    throw 'Protected configuration already exists. Refusing to replace it automatically.'
}

$password = Read-Host 'Enter the PostgreSQL qrbank_app password' -AsSecureString
$credential = New-Object System.Management.Automation.PSCredential('qrbank_app', $password)
$passwordText = $credential.GetNetworkCredential().Password
try {
    $escapedPassword = [System.Uri]::EscapeDataString($passwordText)
    $databaseUrl = 'postgresql+psycopg://qrbank_app:' + $escapedPassword + '@127.0.0.1:5432/qrbank'
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($databaseUrl)
    try {
        Add-Type -AssemblyName System.Security
        $ciphertext = [System.Security.Cryptography.ProtectedData]::Protect(
            $bytes,
            $null,
            [System.Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        [System.IO.File]::WriteAllBytes($secretFile, $ciphertext)
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
} finally {
    Remove-Variable passwordText, escapedPassword, databaseUrl -ErrorAction SilentlyContinue
}

$powershellExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $runner
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $arguments -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCAL SERVICE' -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Local-only QRBank development preview; do not expose to the internet.' | Out-Null

Write-Host "Registered $taskName; not started yet."
Write-Host 'Stop any manually running QRBank preview before starting this task.'
Write-Host "To start: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "To check: Get-ScheduledTask -TaskName '$taskName'"
Write-Host "Logs: $logsDir"
