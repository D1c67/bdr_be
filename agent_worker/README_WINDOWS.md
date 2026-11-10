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

**No such setting?** Some consumer models (many Lenovo IdeaCentre, Legion and
laptop models, for example) do not have one. That is fine for a dev or test
PC: skip A2 and press the power button yourself after a power cut. Nothing is
lost while it is off. Jobs wait in BDR, the worker shows Offline, and the
service picks the jobs up again once Windows starts (no sign-in needed). For
the permanent agent PC, buy hardware that has this setting.

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

**Personal browser pool (worker 1.1.0 and later).** The personal assistant
browser may open any website its owner approves, so a host allowlist on the
office firewall or router cannot list its sites. If the PC has such an
allowlist, widen it for this PC to general outbound HTTPS (443) and HTTP (80),
plus DNS (UDP and TCP 53) to `1.1.1.1`, `8.8.8.8` and `9.9.9.9` and HTTPS to
`1.1.1.1` and `8.8.8.8` (the worker resolves names only through these public
resolvers, never the office DNS server). Keep the Playwright download hosts
above reachable while updates are staged.

The office network is protected on the PC itself instead, in three layers:

1. **Egress proxy (in the signed worker).** Every request of a personal task
   (Chromium, and the worker's own page fetches) goes through a small proxy
   process per task. It resolves names through the public resolvers above,
   refuses every private, loopback, link-local, CGNAT (`100.64.0.0/10`),
   multicast and reserved address (IPv4 and IPv6, including IPv4 written as
   decimal, octal or hex numbers, IPv4-mapped, NAT64 and 6to4 forms), refuses
   `localhost`, single-label names and `.local`, `.lan`, `.internal`,
   `.home.arpa` names, and connects only to the address it checked.
2. **Windows Firewall rules.** Run once, elevated, after installing the user
   pool:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\apply_user_pool_firewall.ps1
   ```

   It blocks the private ranges and the local subnet for the worker's
   `python.exe`, the browser driver `node.exe` and Chromium. The launcher keeps
   the driver and Chromium at fixed paths under
   `C:\ProgramData\BDRAgentUser\runtime\current\`, so updates never make
   the rules stale; a SYSTEM scheduled task ("BDR Agent User Firewall")
   re-applies them at startup and every 15 minutes. If the user pool uses a
   shared Python runtime under Program Files (`-PythonPath`), the rules also
   apply to the org worker's `python.exe`; the org worker never needs the
   office network, so this is harmless. To undo: run the script with
   `-Remove`.

   The office's own public addresses are blocked too: its WAN IPv4 (a page
   could otherwise reach port forwards or the router admin page through
   hairpin NAT) and the routed IPv6 prefixes of the other office VLANs. The
   script looks up the WAN IPv4 (`myip.opendns.com`) and adds the /56 of every
   IPv6 address the PC got from the router. If the office has more than one
   public address, or a known IPv6 site prefix, add them:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\apply_user_pool_firewall.ps1 -OfficeBlockedRanges 203.0.113.7,2001:db8:1200::/48
   ```

   They are kept in `firewall\floor.json` (`extra_blocked`), the scheduled
   task keeps them, the self-test requires them in the rules and the egress
   proxy refuses them. The script sets folder permissions only when run by
   hand; the scheduled task changes rules only.
3. **Boot self-test.** At every start, and before every claim, the worker
   checks the rules name its real program paths and that `python.exe`,
   `node.exe` and Chromium each get the Windows policy error (WSAEACCES) when
   they try to reach the router. If anything fails it keeps running for org
   work and heartbeats but takes no personal browser task (its log says
   why). Rerun the script after replacing the router.

### A5b. Smart App Control off

Windows 11 **Smart App Control** blocks DLLs without a publisher signature,
which includes parts of the worker's Python libraries and Chromium. The install
then stops at "Enrolling this PC with BDR" with **"An Application Control
policy has blocked this file"**.

Check it: **Windows Security > App & browser control > Smart App control
settings**. If it says **On** or **Evaluation**, set it to **Off**. On some
Windows builds it cannot be turned back on without resetting Windows, so do
this only on the agent PC. If the setting is greyed out or already Off and the
block still happens, a company app-control policy (WDAC/AppLocker) is the
cause: ask whoever manages the PC to allow `C:\ProgramData\BDRAgent`.

### A6. Optional: antivirus exclusion

If Microsoft Defender or another antivirus slows the browser down or flags it,
add an exclusion for the folder `C:\ProgramData\BDRAgent` (and, for the
personal pool, `C:\ProgramData\BDRAgentUser`):
**Windows Security > Virus & threat protection > Manage settings > Exclusions >
Add or remove exclusions > Add an exclusion > Folder**.

**Never exclude `C:\ProgramData\BDRQuarantine`**, and never add an exclusion
that covers it (such as `C:\ProgramData` or a wildcard). Files the personal
assistant browser downloads wait there, unopened, until Microsoft Defender
scans them; they are uploaded to BDR only when the scan is clean. The folder is
outside both worker folders on purpose, so the exclusions above never reach it.

Microsoft Defender must stay on for the personal pool. At every worker start
the self-test writes the harmless EICAR antivirus test file into the
quarantine and requires Defender to catch it; this is why Windows Security
shows a "Threat found: EICAR test file" entry after each restart of the
BDRAgentUser service. If Defender is off, or an exclusion covers the
quarantine, the worker refuses every download (personal tasks still run, and
the task result says the file was refused).

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
   If the PC has no such setting (see A2), press the power button.
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

### Personal browser worker pool

The personal worker runs as a second Windows service and local account. Use a
separate enrollment code minted for the user pool; a code for the org pool does
not authorize personal tasks. Do not reuse an installed worker token.

```powershell
powershell -ExecutionPolicy Bypass -File .\bdr-install.ps1 -Pool user -Code <USER_POOL_CODE>
```

Personal workers require signed release **1.0.1 or later** selected by BDR.
An installer carrying 1.0.0 stops before changing the service or its files.

On a PC whose org worker owns `C:\ProgramData\BDRAgent\python`, the Python
3.12 MSI cannot create another installation of the same runtime at a second
path. The installer stops before changing either worker. Prepare the official
Python 3.12.10 x64 NuGet runtime in an administrator-managed directory under
Program Files, then pass its executable with `-PythonPath`. This standalone
runtime avoids MSI installation and leaves the org worker's runtime and HKLM
registration in place. The NuGet package includes `venv`, `ensurepip` and pip;
the launcher creates its own virtual environment from this runtime.
The installer checks signature, ownership, ACLs, reparse points, architecture
and required modules before using it. It never copies the org runtime or its data.
The shared runtime is not owned or removed by the personal service uninstaller.
For an existing protected runtime registered in HKLM, `-UseExistingPython`
remains available; `-PythonPath` bypasses that registry lookup.

For the current development setup, run
`bash scripts/prepare_phase6_windows_install.sh` on the backend Mac. It verifies
the published release and Python package, publishes the runtime setup and
installer, then prints the exact Windows commands. Keep that Mac terminal open
to retain the development tunnel. Use a fresh **Personal tasks** enrollment
code and verify the release fingerprint against the trusted copy. The runtime
setup checks a real virtual environment and pip before installing its directory.

```powershell
powershell -ExecutionPolicy Bypass -File .\bdr-install.ps1 -Pool user -PythonPath 'C:\Program Files\BDR Python 3.12.10\tools\python.exe' -Code <USER_POOL_CODE>
```

The installer selects service `BDRAgentUser`, account `bdr-agent-user`, and data
home `C:\ProgramData\BDRAgentUser`. Its DPAPI token, Chromium process, temporary
profile, logs, release state and ACLs are separate from `BDRAgent` / `bdr-agent`.
The signed release can contain both adapters; the authenticated server pool and
allowed types determine which one runs. The org pool keeps `rfi_from_email` and
`canary`; the user pool receives only `user_browser` (release 1.1.0 and
later; the retired `user_browser_read` is refused). Pool identity cannot be
changed by the worker or by editing local settings.

After enrollment, approve the personal worker and allow **Personal browser**. **Run self-test** runs the organization canary and does not apply to
the personal pool. An organization self-test requires an online organization
worker with `canary` allowed and the **Worker self-test** type enabled. If the PC
is powered on but BDR still shows Offline, inspect the appropriate Windows
service and its `logs\worker.log`;
power alone does not confirm a connection to the configured BDR API.

In the type settings, a successful **Test model** does not fill in prices.
Use **Edit** to save both input and output prices per million tokens before
**Enable** becomes available. Enabling the type alone does not complete
personal browser setup: the backend feature configuration, encryption key,
personal worker release and owner credential must also be ready. The Procore
password in organization Browser agents settings does not populate the
owner's personal credential vault.

Personal task steps spool only in bounded process memory and upload to the
private API. A crash loses unacknowledged steps. Personal screenshots, help and
model traces use encrypted owner records on BDR. Each personal task runs in
its own Chromium process with its own temporary profile under
`C:\ProgramData\BDRAgentUser\jobs\` and its own egress proxy (A5), and both
are removed when the task ends or at the next service start. Sign-in uses
`fill_credential`: the worker writes the saved username and password into a
field on one of the credential's login origins only (never in a cross-origin
frame, never on an identity provider such as Microsoft or Google sign-in) and
no login screenshots are uploaded; the model never sees the values. Final
actions (submit, send, delete, pay and similar) are held at the network until
the owner approves them in chat, and a new site is held until the owner
approves it. Downloads wait in `C:\ProgramData\BDRQuarantine` for a Defender
scan. Windows disk encryption and service ACL checks remain deployment
acceptance items.

Re-enroll or uninstall the personal service with the same pool selection:

```powershell
powershell -ExecutionPolicy Bypass -File .\bdr-install.ps1 -Pool user -Reenroll -Code <USER_POOL_CODE>
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Pool user
```

Feature flags remain off until the dedicated service is installed, pool
enrollment is verified, and the local Chromium fixture plus real-account
read-only acceptance checks pass. The fixture commands are:

```powershell
python -m pytest tests/test_user_browser_v2_egress.py tests/test_user_browser_v2_guard.py tests/test_user_browser_v2_selftest.py tests/test_user_browser_v2_tools.py tests/test_user_browser_retired_v1.py -q
```

The fixtures use a loopback TLS proxy and no external credentials. A test host
must permit local socket binding and Chromium process startup. Their checks
include private-network refusals in the egress proxy, held final actions and
site holds, refused redirects that never reach the destination server, the
exfiltration rules, and sign-in with `fill_credential` using only private
worker endpoints.

### Prepare release 1.0.1 on the build Mac

From `bdr_be`, use a build environment with pip and packaging explicitly
available; a uv project environment does not necessarily contain pip:

```sh
UV_CACHE_DIR=/private/tmp/bdr-release-uv uv run --with pip --with packaging python agent_worker/tools/build_release_bundle.py --version 1.0.1 --out /private/tmp/bdr-agent-release-1.0.1
```

This downloads CPython 3.12 Windows x64 wheels and validates the exact project
pins, Windows ABI/platform tags, metadata, and complete transitive dependency
closure before creating the zip. Existing Windows wheels can be supplied with
`--wheels-dir <directory>`; `--check-wheels` validates them without creating a
bundle. Missing packages, incompatible wheels, and unexpected extra packages
are refused. The zip contains a hash-pinned offline requirements file.

The build prints the local artifact path and SHA-256. Signing and publishing
are separate steps; producing the zip does not change BDR's selected release.
Rebuilding with the same source and wheel bytes produces the same artifact.
