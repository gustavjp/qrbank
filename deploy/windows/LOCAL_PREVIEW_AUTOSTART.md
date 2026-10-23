# Local-only Windows autostart (preview, not production)

QRBank currently runs only while its PowerShell console is open. This optional
Task Scheduler setup keeps the **development preview** running after a Windows
restart. It does **not** configure HTTPS, public DNS, real email delivery,
monitoring, tested backups, or Windows activation. Do not use real student data.

It uses the built-in Windows Task Scheduler, running as the low-privilege
`LOCAL SERVICE` account, rather than installing a third-party service wrapper.
The PostgreSQL connection string is encrypted by Windows DPAPI for the local
machine and saved beneath `C:\ProgramData\QRBank\secrets`. NTFS ACLs limit its
access to Administrators, SYSTEM, and LOCAL SERVICE. Machine-bound DPAPI
protects secrets at rest, but it is **not** a defense against someone with
administrator or LOCAL SERVICE execution privileges. Do not copy the encrypted
file to another server expecting it to decrypt.

## Before installing

1. Install prerequisites and verify the application locally by following
   `deploy/windows/README.md`. `/health` and `/ready` must pass.
2. Verify `Get-Service -Name 'postgresql*'` shows a Running/Automatic service.
3. Open **PowerShell as Administrator** on the VPS in `C:\Apps\qrbank`.
4. Stop the manually started `uvicorn` preview with Ctrl+C so port 8000 is free.

## Register, start, verify

```powershell
Set-Location C:\Apps\qrbank
.\deploy\windows\install_local_preview_task.ps1
```

The installer prompts for the **qrbank_app PostgreSQL password** without
displaying it. It refuses to replace an existing `QRBankLocalPreview` task or
existing encrypted database file. It creates a task but does not start it yet.

```powershell
Start-ScheduledTask -TaskName 'QRBankLocalPreview'
Get-ScheduledTask -TaskName 'QRBankLocalPreview'
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Allow a few seconds for startup. Look under `C:\ProgramData\QRBank\logs` if
either endpoint fails. Logs may contain development-only email verification or
reset links and should not be shared publicly.

To stop the preview:

```powershell
Stop-ScheduledTask -TaskName 'QRBankLocalPreview'
```

After reboot, repeat the two local endpoint checks to verify startup without
any logged-in user. Task Scheduler is configured to attempt three restarts at
one-minute intervals after an unexpected failure. This is not yet a full
monitoring or recovery system.

**Keep TCP 8000 and TCP 5432 closed in the public firewall.** The web process
listens on `127.0.0.1` only. A domain/HTTPS reverse proxy and transactional
email setup must be completed before real classroom accounts are allowed.
