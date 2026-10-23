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

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'QRBank Python environment was not found.'
    }
    if (-not (Test-Path -LiteralPath $secretFile -PathType Leaf)) {
        throw 'Protected QRBank database configuration was not found.'
    }

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

    $env:QRBANK_ENVIRONMENT = 'development'
    $env:QRBANK_PUBLIC_BASE_URL = 'http://localhost:8000'
    $env:QRBANK_EMAIL_DELIVERY_MODE = 'console'
    $env:QRBANK_SESSION_COOKIE_SECURE = 'false'

    Set-Location -LiteralPath $repoRoot
    # Preflight has no external side effects and intentionally blocks production.
    & $python 'deploy\check_environment.py' | Out-File -LiteralPath $outputLog -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        throw 'QRBank configuration preflight failed.'
    }

    # Keep the web app bound to loopback. Logs may contain development email links.
    $process = Start-Process -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $outputLog `
        -RedirectStandardError $errorLog `
        -PassThru -Wait

    # Always signal failure so the scheduler attempts to restart an unexpected exit.
    exit 1
} catch {
    # Never print environment variables or database URLs to logs.
    $message = '[{0}] QRBank preview failed: {1}' -f (Get-Date -Format 's'), $_.Exception.GetType().Name
    $message | Out-File -LiteralPath $errorLog -Append -Encoding utf8
    exit 1
}
