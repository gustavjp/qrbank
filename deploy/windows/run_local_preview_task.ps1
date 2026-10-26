# QRBank localhost-only preview runner. Launched by Task Scheduler as LOCAL SERVICE.
# Do not execute with a real production account or enable public access.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$dataRoot = Join-Path $env:ProgramData 'QRBank'
$secretFile = Join-Path $dataRoot 'secrets\database_url.bin'
$logDirectory = Join-Path $dataRoot 'logs'
$logStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outputLog = Join-Path $logDirectory "preview-$logStamp.stdout.log"
$errorLog = Join-Path $logDirectory "preview-$logStamp.stderr.log"
$preflightLog = Join-Path $logDirectory "preview-$logStamp.preflight.log"
$preflightErrorLog = Join-Path $logDirectory "preview-$logStamp.preflight.stderr.log"
$stage = 'checking-python'

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'QRBank Python environment was not found.'
    }
    $stage = 'checking-protected-config'
    if (-not (Test-Path -LiteralPath $secretFile -PathType Leaf)) {
        throw 'Protected QRBank database configuration was not found.'
    }

    $stage = 'decrypting-protected-config'
    Add-Type -AssemblyName System.Security
    $ciphertext = [System.IO.File]::ReadAllBytes($secretFile)
    $plaintext = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $ciphertext,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    try {
        $env:QRBANK_DATABASE_URL = [System.Text.Encoding]::UTF8.GetString($plaintext)
    } finally {
        [Array]::Clear($plaintext, 0, $plaintext.Length)
    }

    $stage = 'configuring-preview-environment'
    $env:QRBANK_ENVIRONMENT = 'development'
    $env:QRBANK_PUBLIC_BASE_URL = 'http://localhost:8000'
    $env:QRBANK_EMAIL_DELIVERY_MODE = 'console'
    $env:QRBANK_SESSION_COOKIE_SECURE = 'false'

    Set-Location -LiteralPath $repoRoot
    # Preflight has no external side effects and intentionally blocks production.
    $stage = 'running-preflight'
    $preflightProcess = Start-Process -FilePath $python `
        -ArgumentList @('deploy\check_environment.py') `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $preflightLog `
        -RedirectStandardError $preflightErrorLog `
        -PassThru -Wait
    if ($preflightProcess.ExitCode -ne 0) {
        throw 'QRBank configuration preflight failed.'
    }

    # Keep the web app bound to loopback. Logs may contain development email links.
    $stage = 'starting-web-process'
    $process = Start-Process -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $outputLog `
        -RedirectStandardError $errorLog `
        -PassThru -Wait

    # Always signal failure so the scheduler attempts to restart an unexpected exit.
    exit 1
} catch {
    # Stage and exception type are safe to record; never print credentials or URLs.
    $message = '[{0}] QRBank preview failed at {1}: {2}' -f (Get-Date -Format 's'), $stage, $_.Exception.GetType().Name
    $message | Out-File -LiteralPath $errorLog -Append -Encoding utf8
    exit 1
}
