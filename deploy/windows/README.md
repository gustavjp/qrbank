# QRBank on a Windows Server VPS — preparation and local preview

This checklist targets an OVHcloud Windows Server 2025 VPS. It prepares QRBank for
deployment **without publishing the app to the internet**. The current app is
**not ready for real teachers or student data** until an actual SMTP provider,
HTTPS, DNS/domain verification, backups, and mobile-camera device testing are
completed and verified.

## 1. Finish and secure Windows first

1. Complete Windows Update and restart. Check again until no important updates remain.
2. Make sure you can sign in over RDP and can recover access from the OVHcloud
   browser-based KVM console.
3. Keep Windows Defender Firewall enabled. **Do not open TCP 8000 or PostgreSQL
   port 5432** to the internet. Limit RDP access to trusted source addresses or
   put it behind a private VPN; test console recovery before changing RDP rules.
4. Plan separate off-server database backups and test a restore before storing
   meaningful data. VPS availability is not the same as backup.

Do not share server passwords, database connection strings, or RDP screenshots
that reveal credentials in GitHub or chat.

## 2. Install prerequisites

From their official distributors, install:

- Python **3.13 x64** (or another supported Python 3.12+), including the `py` launcher.
- Git for Windows.
- PostgreSQL for Windows, with the server listening only on loopback and its
  service configured to start automatically. PostgreSQL 16 matches CI today.

Check Python and Git in a new PowerShell window:

```powershell
py -3.13 --version
git --version
Get-Service -Name 'postgresql*'
```

Create a database named `qrbank` and a dedicated login/owner such as
`qrbank_app` with a strong unique password using PostgreSQL administration
tools. Keep that password out of your repository. Do not use the sample
`qrbank:qrbank` credentials from the development README.

## 3. Prepare the application

Use PowerShell in an ordinary working directory (for example `C:\Apps`). You
need legitimate GitHub access to clone the private repository.

```powershell
New-Item -ItemType Directory -Force C:\Apps | Out-Null
Set-Location C:\Apps
git clone https://github.com/gustavjp/qrbank.git
Set-Location C:\Apps\qrbank
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

This is a basic preview setup, **not** a Windows service. Closing the PowerShell
session or signing out will stop the preview. Automated startup, upgrades and
monitoring will be a later deployment milestone.

## 4. Configure a temporary, local-only preview

In the same PowerShell window, enter the database URL *without displaying it or
putting it in command history*. The format is
`postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/qrbank`; percent-encode
special characters inside USER/PASSWORD as needed.

```powershell
$secret = Read-Host 'Enter the full QRBANK_DATABASE_URL' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $env:QRBANK_DATABASE_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
$env:QRBANK_ENVIRONMENT = 'development'
$env:QRBANK_PUBLIC_BASE_URL = 'http://localhost:8000'
$env:QRBANK_EMAIL_DELIVERY_MODE = 'console'
$env:QRBANK_SESSION_COOKIE_SECURE = 'false'
```

These variables exist in the current PowerShell process and its child
processes; they are not a persistent production secret-management solution.
Development email-verification and password-reset links print in server logs,
so keep this preview private and use only dummy accounts.

Run the read-only configuration preflight before connecting to PostgreSQL:

```powershell
.\.venv\Scripts\python.exe deploy\check_environment.py
```

The preflight reports errors without printing the database connection string.
For production, it requires an HTTPS public URL and SMTP configuration. A
successful check does **not** prove PostgreSQL or SMTP are reachable, that
domain verification is complete, or that the app is safe to publish.

## 5. Migrate and test on localhost

Once PostgreSQL is running and the new database exists:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Keep that window open. On the VPS itself, visit `http://127.0.0.1:8000/health`
and `http://127.0.0.1:8000/ready`; both should succeed. `/health` only checks
the web process; `/ready` also checks database connectivity. You can also run
these from another PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Press **Ctrl+C** in the server window to stop the preview.

## Later: configure production SMTP (not for this local preview)

The application can use an SMTP provider supporting authenticated TLS. Set
`QRBANK_EMAIL_DELIVERY_MODE=smtp` alongside:

| Variable | Purpose |
| --- | --- |
| `QRBANK_SMTP_HOST` | Provider's SMTP hostname |
| `QRBANK_SMTP_PORT` | Usually `587` with STARTTLS or `465` with SSL |
| `QRBANK_SMTP_SECURITY` | `starttls` (default) or `ssl`; no plaintext mode |
| `QRBANK_SMTP_USERNAME` | Provider's SMTP credential username |
| `QRBANK_SMTP_PASSWORD` | Provider's SMTP secret or app password |
| `QRBANK_SMTP_FROM_ADDRESS` | Verified sender mailbox, such as `no-reply@your-domain` |

Never commit SMTP credentials or put them in PowerShell history. Configure
`QRBANK_ENVIRONMENT=production`, `QRBANK_PUBLIC_BASE_URL` to the real HTTPS
site origin, and secure session cookies. Verify SPF, DKIM and DMARC per your
provider's instructions, and test actual inbox delivery before opening signups.

## Next milestones — not included in this PR

1. Configure domain/DNS, an HTTPS reverse proxy on port 443, and production
   server startup/restart handling. Keep the Python app on `127.0.0.1` and
   PostgreSQL unexposed. Never expose port 8000 directly.
2. Provision a transactional SMTP account, verify sender/domain identity and
   deliverability, and test registration/password reset end to end. SMTP code
   alone does not mean real mail can be sent yet.
3. Add off-server database backups, monitoring and an upgrade/rollback runbook.
4. Test QR camera scanning on an actual HTTPS-connected phone. Configure weekly
   wage/allowance Windows scheduled tasks only once classroom setup is ready.
5. Configure Minecraft separately with its own service, backups and firewall
   rules after QRBank's security and resource usage are established.

**Never open teacher registration or enter real student information during this
preview phase.**
