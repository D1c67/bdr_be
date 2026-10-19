# BDR Agent on the Windows PC: owner instructions

This PC runs the BDR browser agent: a Windows service called **BDR Agent** that
picks up jobs from BDR (for example, reading a Procore RFI after an RFI email
arrives), works them in a hidden Chromium browser, and reports back. It needs
nobody signed in. Everything about it (which jobs it may run, the AI model,
spending caps, the Procore password, the code version) is set in BDR, not on
the PC.

You touch the PC only for:

- the one-time setup below;
- re-enrolling after IT Admin revokes it (one installer run);
- checking it after hardware work or a long power cut.

Code updates arrive automatically. They are signed, and the PC refuses any
update that is not.

---

## What you need

- The PC: Windows 11 Pro, 64-bit, wired network, always on.
- A Windows account with administrator rights on that PC.
- The password manager entry **"BDR agent release key fingerprint"**, saved
  when the release key was made on the Mac. It is 16 groups of 4 characters,
  for example `3F2A 91C0 ...`.
- An IT Admin login to BDR (Settings > Browser agents).

---

## Part A: prepare the PC (once)

### A1. Windows basics

1. Run Windows Update (**Settings > Windows Update**) until nothing is left,
   restarting as asked.
2. Turn on BitLocker: **Settings > Privacy & security > Device encryption**
   (or **Control Panel > BitLocker Drive Encryption**). Save the recovery key in
   the password manager.
3. Turn on automatic time: **Settings > Time & language > Date & time > Set
   time automatically: On**. Logins and signed links fail when the clock is off.

### A2. BIOS: power back on after a power cut

The PC must start by itself when the power returns.

1. Restart the PC and open the BIOS/UEFI setup. The key shows briefly at power
   on: usually **F2**, **Del**, **F10** or **F1**. On Windows 11 you can also go
   to **Settings > System > Recovery > Advanced startup > Restart now**, then
   **Troubleshoot > Advanced options > UEFI Firmware Settings**.
2. Find the power setting. Its name depends on the maker:
   - Dell: **Power Management > AC Recovery** (or "AC Behavior")
   - HP: **Advanced > Boot Options > After Power Loss**
   - Lenovo: **Power > After Power Loss**
   - ASUS / MSI / Gigabyte boards: **Restore AC Power Loss** or **AC Power Recovery**
3. Set it to **Power On** (or **On**). "Last State" also works, but Power On is
   safer.
4. If there is a **Wake on LAN** setting, you may turn it on as well.
5. Save and exit (usually **F10**).

Test it once: with Windows running, pull the power cord, wait 10 seconds and
plug it back in. The PC should start by itself.

### A3. Never sleep, never hibernate, Fast Startup off

Open **PowerShell as Administrator** (Start, type `powershell`, right-click
**Windows PowerShell**, **Run as administrator**) and paste:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change monitor-timeout-ac 15
powercfg /hibernate off
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f
```

What this does:

- Sleep and hibernate are never used. The screen may still turn off, which is fine.
- `powercfg /hibernate off` also turns off Fast Startup. The last line makes
  sure it stays off. With Fast Startup on, Windows "shuts down" into a
  half-hibernated state and the service can come back in a confused state.

To check: **Control Panel > Power Options > Choose what the power buttons do**.
"Turn on fast startup" must be unticked (or missing).

### A4. Windows Update restarts

**Settings > Windows Update > Advanced options > Active hours**: set them to
your business hours, for example 6:00 to 20:00. Windows then restarts for
updates outside those hours. The service comes back by itself after a restart,
and BDR hands any interrupted job to the PC again.

### A5. Outbound network allowlist

The agent only makes outgoing HTTPS connections (port 443). Nothing needs to
reach the PC from outside, so no inbound ports are opened.

Windows Defender Firewall cannot filter by host name. If you want an outbound
allowlist, set it on the office firewall or router (most business firewalls,
for example UniFi, Meraki or FortiGate, support domain or FQDN rules). Allow
these for the PC:

Always needed:

| Host | Why |
|---|---|
| your BDR API host (the address in BDR's Add worker dialog) | jobs, heartbeats, updates |
| `<project>.supabase.co` (the storage host in that dialog) | downloading signed updates |
| `*.procore.com`, `*.procoretech.com`, `*.procorecdn.com` | Procore |
| `challenges.cloudflare.com` | the check pages Procore sometimes shows |
| `www.google.com`, `www.gstatic.com` | captcha scripts on login pages |
| `*.amazonaws.com` | Procore attachment storage |
| `cdn.playwright.dev`, `playwright.download.prss.microsoft.com`, `playwright.azureedge.net` | the Chromium download, during installs and some updates |

Only during the first install:

| Host | Why |
|---|---|
| `www.python.org` | the Python 3.12 installer |
| `github.com`, `objects.githubusercontent.com` | WinSW, the service wrapper |

No AI provider addresses are needed: BDR makes every AI call itself.

Also leave Windows Update and time sync (`time.windows.com`) reachable.

### A6. Optional: antivirus exclusion

If Microsoft Defender or another antivirus slows the browser down or flags it,
add an exclusion for the folder `C:\ProgramData\BDRAgent`:
**Windows Security > Virus & threat protection > Manage settings > Exclusions >
Add or remove exclusions > Add an exclusion > Folder**.

---

## Part B: in BDR (IT Admin)

1. **Settings > Browser agents > Workers > Add worker.** Give it a name, for
   example `office-agent-1`.
2. BDR shows:
   - a **20 character enrollment code**. It works once and expires after
     30 minutes, so do Part C straight away;
   - the **install command**;
   - the **release key fingerprint**.
3. Check that the fingerprint BDR shows matches the password manager entry. If
   it does not, stop and do not install.

---

## Part C: install on the PC

1. Sign in to the PC with your administrator account.
2. Open **PowerShell as Administrator** (see A3).
3. Paste the install command from BDR. It looks like this:

   ```powershell
   irm https://<bdr-api>/agent-worker/v1/installer.ps1 -OutFile $env:TEMP\bdr-install.ps1
   powershell -ExecutionPolicy Bypass -File $env:TEMP\bdr-install.ps1
   ```

   Run it right away: the installer carries a download link that lasts
   15 minutes.

4. The installer prints:

   ```
   Release public key fingerprint:
       3F2A 91C0 ....
   ```

   Compare **every group** with the password manager entry. If even one
   character differs, type `no` and tell IT Admin. If it matches, type `yes`.

5. Paste the enrollment code when asked. Dashes and spaces are ignored.
6. Wait. The first install takes about 5 to 10 minutes: it installs Python,
   the worker and Chromium. You will see these steps:
   `Getting the worker release`, `Python 3.12.10 runtime`,
   `Service user bdr-agent`, `Permissions`,
   `Verifying and installing the worker release`,
   `Enrolling this PC with BDR`, `Registering the BDR Agent service`.
7. It ends with **DONE. The worker is running on this PC.**

If it says **STOPPED:**, read the message. Nothing half-done is left running.
The most common causes:

- the code expired (get a new one, then run again with `-Reenroll`);
- the download link expired (download the installer again);
- a blocked host (see A5).

**Offline install** (no installer download link, for example over a slow
connection): IT Admin gives you the release zip and its `release.json` on a USB
stick. Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\bdr-install.ps1 -BundlePath E:\bdr-agent-1.0.0.zip -ReleaseJsonPath E:\release.json
```

---

## Part D: check it shows online in BDR

1. In BDR, **Settings > Browser agents > Workers**: the new PC is listed as
   **Pending**. Click **Approve**.
2. Set its **allowed types** (for example `canary` and `rfi_from_email`).
   Optionally set **allowed IPs** to the office's public IP.
3. Within about 30 seconds the status turns **Online** (green): BDR has heard
   a heartbeat in the last 30 seconds. The version and Chromium version appear
   next to it.
4. Click **Run self-test**. The canary job should finish **Succeeded** within a
   minute or two.

To check on the PC itself (PowerShell as Administrator):

```powershell
Get-Service BDRAgent                         # Status should be Running
& "C:\ProgramData\BDRAgent\python\python.exe" -I "C:\ProgramData\BDRAgent\launcher\launcher.py" status
Get-Content C:\ProgramData\BDRAgent\logs\launcher.log -Tail 20
Get-Content C:\ProgramData\BDRAgent\logs\worker.log -Tail 40
```

`status` shows the current version, the previous one, any versions marked bad,
and the last decisions the launcher made.

---

## After maintenance

Use this list after a power cut, Windows updates, hardware work, a BIOS update
or moving the PC.

1. **Is the PC on?** If it stayed off after a power cut, the BIOS setting (A2)
   is wrong or was reset. A BIOS update often resets it, so check A2 again.
2. **Is it Online in BDR?** Allow about 3 minutes after Windows starts (the
   service starts "delayed" so the network is up first). You do not need to
   sign in.
3. **Not online after 5 minutes?** On the PC, in PowerShell as Administrator:

   ```powershell
   Get-Service BDRAgent
   Start-Service BDRAgent
   Get-Content C:\ProgramData\BDRAgent\logs\launcher.log -Tail 30
   ```

4. **Sleep or Fast Startup came back** (after a big Windows feature update):
   run the A3 commands again.
5. **The PC moved, or the office internet changed:** if IT Admin set allowed
   IPs, they must update them to the new public IP in BDR. Otherwise the worker
   is refused (`ip_not_allowed` in `worker.log`).
6. **Jobs that were running when the PC went down** are handed back to BDR and
   run again on their own. Nothing to do.

---

## Updates (automatic)

- IT Admin picks the **desired version** in BDR. The PC finishes or hands back
  its running jobs (at most 10 minutes), downloads the update and checks its
  signature against the key installed in Part C. It then installs the update
  next to the current version and restarts into it.
- The PC refuses an update that is not signed by the release key, is older
  than a signed minimum version, or was marked bad on this PC.
- **Automatic rollback.** The PC rolls back to the previous version and marks
  the new one **bad** when the new version:
  - does not start properly within 3 minutes (it starts its browser and reads
    its settings);
  - crashes 3 times within 10 minutes.

  BDR shows bad versions on the worker's row. This check uses only the PC
  itself, so a BDR or internet outage never causes a rollback.
- The launcher keeps the current version, the previous one and at most one
  more under `C:\ProgramData\BDRAgent\versions`.

---

## Re-enroll

Re-enroll when:

- IT Admin clicked **Revoke** (for example, the PC or its token may have been
  compromised); or
- `worker.log` says the token is unreadable or the worker is not enrolled; or
- the service was broken by a Windows reinstall.

Steps:

1. In BDR: **Revoke** the old worker row if it is not revoked yet, then
   **Add worker** for a new code (Part B).
2. On the PC, in PowerShell as Administrator, download the installer from the
   new command in BDR and run it with `-Reenroll`:

   ```powershell
   irm https://<bdr-api>/agent-worker/v1/installer.ps1 -OutFile $env:TEMP\bdr-install.ps1
   powershell -ExecutionPolicy Bypass -File $env:TEMP\bdr-install.ps1 -Reenroll
   ```

3. Compare the fingerprint again, type `yes`, and paste the new code.
4. In BDR, **Approve** the new row, set its allowed types, and **Run self-test**
   (Part D).

`-Reenroll` keeps the installed versions. It replaces the launcher, the
release key and the token, and gives the `bdr-agent` user a new random
password. If the PC may have been compromised, also change the Procore
password in BDR (**Settings > Browser agents > Procore password**).

---

## Uninstall

1. In BDR, **Revoke** the worker.
2. On the PC, in PowerShell as Administrator. The installer keeps a copy of
   the uninstaller in `C:\ProgramData\BDRAgent\service\uninstall.ps1`. Copy it
   out first, because it deletes that folder:

   ```powershell
   Copy-Item C:\ProgramData\BDRAgent\service\uninstall.ps1 $env:TEMP\bdr-uninstall.ps1
   powershell -ExecutionPolicy Bypass -File $env:TEMP\bdr-uninstall.ps1
   ```

   PCs installed before the uninstaller was bundled have no copy: take
   `agent_worker\install\uninstall.ps1` from the BDR repo, or rerun the
   current installer with `-Reenroll` once to place it.

   This removes the service, the `bdr-agent` user and its profile, the private
   Python and `C:\ProgramData\BDRAgent`. Use `-KeepData` to keep the logs.

---

## Troubleshooting

| Symptom | Where to look | What to do |
|---|---|---|
| Offline in BDR, service Running | `logs\worker.log` | `unauthorized` or `revoked`: re-enroll. `ip_not_allowed`: update allowed IPs in BDR. Connection errors: check the firewall (A5). |
| Offline in BDR, service Stopped | `logs\service\*.log`, `logs\launcher.log` | `Start-Service BDRAgent`. If it stops again, send IT Admin the last 50 lines of both logs. |
| Pending in BDR | | IT Admin clicks Approve. |
| `no installed worker version can run` in `launcher.log` | `launcher.py status` | Every installed version is bad or below the signed minimum. IT Admin publishes a fixed version, then you rerun the installer with `-Reenroll`. |
| Installer: `The release was REJECTED` | | The download does not match the signature. Do not retry. Tell IT Admin; the installer or download may have been tampered with. |
| Installer: Python already installed elsewhere | | Run again with `-UseExistingPython` (uses a machine-wide Python 3.12 under Program Files). |
| PC did not start after a power cut | BIOS | Set the A2 setting again (BIOS updates reset it). |

### Reference

```
C:\ProgramData\BDRAgent\
  launcher\        stable launcher + release_key.pub (read-only to bdr-agent)
  python\          Python 3.12 runtime (read-only to bdr-agent)
  service\         BDRAgent.exe (WinSW 2.12), BDRAgent.xml (no password), install.json
  versions\x.y.z\  side-by-side worker versions
  state\           launcher.json (current, previous, bad versions), pending.txt
  ms-playwright\   Chromium
  logs\            launcher.log, worker.log, service\ (WinSW)
  worker.json      BDR address and worker id (never edit)
  token.bin        worker token, DPAPI-protected for bdr-agent
```

Worker exit codes, as seen in `launcher.log`:

| Code | Meaning |
|---|---|
| 0 | stopped |
| 1 | crash; counts toward rollback |
| 2 | not enrolled or local setup problem |
| 3 | another copy took over; restarts at once |
| 4 | revoked; re-enroll |
| 5 | update staged; restarts into it |
