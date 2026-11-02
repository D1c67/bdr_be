<#
.SYNOPSIS
    Installs the BDR browser agent worker on this Windows PC as the "BDR Agent" service.

.DESCRIPTION
    Run once, as Administrator, in Windows PowerShell 5.1 (64-bit) or PowerShell 7:

        powershell -ExecutionPolicy Bypass -File .\bdr-install.ps1

    What it does, in order:
      1. Shows the release public key fingerprint and waits for you to type yes
         after comparing it with the copy in the password manager. Nothing on
         the PC changes before that.
      2. Downloads the signed worker release and checks its sha256.
      3. Installs Python 3.12 (official python.org installer, sha256 and
         Authenticode checked) into C:\ProgramData\BDRAgent\python, or uses an
         existing machine-wide Python 3.12 with -UseExistingPython.
      4. Creates the standard local user bdr-agent (random password nobody
         needs to know; hidden from the sign-in screen).
      5. Locks down C:\ProgramData\BDRAgent: bdr-agent can write its data, but
         the launcher, the Python runtime, the service files and the release
         key are read-only to it.
      6. The launcher verifies the release (Ed25519 signature with the baked-in
         key, sha256, min_version) and installs it side by side, including
         Playwright's Chromium.
      7. Enrolls with the one-time code, running as bdr-agent so the token is
         protected by Windows DPAPI for that user.
      8. Registers the WinSW service (automatic delayed start, restart on
         failure) and starts it.

    Re-enroll (after IT Admin revoked the worker, or the token was lost):

        powershell -ExecutionPolicy Bypass -File .\bdr-install.ps1 -Reenroll

.PARAMETER Code
    The 20 character enrollment code from BDR Settings > Browser agents > Add worker.
    Asked for when not given.

.PARAMETER Reenroll
    Replace the token of an installed worker with a new code. Also refreshes the
    launcher, the release key and the service account password.

.PARAMETER BundlePath
    Offline install: a release zip (bdr-agent-x.y.z.zip). Needs -ReleaseJsonPath.

.PARAMETER ReleaseJsonPath
    Offline install: the release's {"manifest": ..., "signature": ...} JSON.

.PARAMETER UseExistingPython
    Use a protected machine-wide Python 3.12 x64 registered in HKLM instead of
    installing a private copy.

.PARAMETER PythonPath
    Use this explicit python.exe under Program Files instead of reading HKLM.
    Implies -UseExistingPython. The runtime must be administrator-owned, signed
    by the Python Software Foundation, and include venv and ensurepip.
#>
[CmdletBinding()]
param(
    [ValidateSet('org', 'user')]
    [string]$Pool = 'org',
    [string]$Code,
    [switch]$Reenroll,
    [string]$ApiBase,
    [string]$BundlePath,
    [string]$ReleaseJsonPath,
    [string]$InstallRoot = (Join-Path $env:ProgramData 'BDRAgent'),
    [switch]$UseExistingPython,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# Filled in when BDR renders this script (tools/render_installer.py). When left
# unfilled, the files next to this script are used (repository layout).
$EmbeddedApiBase     = '__BDR_API_BASE__'
$EmbeddedPubKeyB64   = '__RELEASE_PUBKEY_B64__'
$EmbeddedLauncherB64 = '__LAUNCHER_ZIP_B64__'
$EmbeddedWinswXmlB64 = '__WINSW_XML_B64__'
$EmbeddedReleaseB64  = '__RELEASE_JSON_B64__'

$ServiceId   = 'BDRAgent'
$ServiceUser = 'bdr-agent'
if ($Pool -eq 'user') {
    $ServiceId = 'BDRAgentUser'
    $ServiceUser = 'bdr-agent-user'
    if (-not $PSBoundParameters.ContainsKey('InstallRoot')) {
        $InstallRoot = Join-Path $env:ProgramData 'BDRAgentUser'
    }
}
# A second pool must never share credentials, writable data or a service account.
$otherRoot = Join-Path $env:ProgramData $(if ($Pool -eq 'user') { 'BDRAgent' } else { 'BDRAgentUser' })
$installFull = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$otherFull = [IO.Path]::GetFullPath($otherRoot).TrimEnd('\')
if ($installFull.Equals($otherFull, [StringComparison]::OrdinalIgnoreCase) -or
    $installFull.StartsWith($otherFull + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $otherFull.StartsWith($installFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Worker pool installation roots must not overlap.'
}


# Pinned downloads (sha256 measured 2026-10-19). Change only together with the hash.
$PythonVersion = '3.12.10'
$PythonUrl     = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$PythonSha256  = '67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb'
$WinswUrl      = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinswSha256   = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'

$SidAdministrators = 'S-1-5-32-544'
$SidSystem         = 'S-1-5-18'
$SidUsers          = 'S-1-5-32-545'

# Helpers ---------------------------------------------------------------------------------

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Stop-Install([string]$Message) {
    Write-Host ''
    Write-Host "STOPPED: $Message" -ForegroundColor Red
    exit 1
}

function Test-Filled([string]$Value) {
    return ($Value -and -not ($Value.StartsWith('__') -and $Value.EndsWith('__')))
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Invoke-Download([string]$Url, [string]$Dest) {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Dest
}

function Get-RawKeyFromText([string]$Text) {
    $Text = $Text.Trim()
    if ($Text.StartsWith('-----BEGIN')) {
        $body = ($Text -split "`r?`n" | Where-Object { $_ -and -not $_.StartsWith('-----') }) -join ''
        $der = [Convert]::FromBase64String($body)
        $prefix = '302a300506032b6570032100'
        $hex = ([BitConverter]::ToString($der[0..11])).Replace('-', '').ToLowerInvariant()
        if ($der.Length -ne 44 -or $hex -ne $prefix) { Stop-Install 'The release key is not an Ed25519 public key.' }
        return [byte[]]$der[12..43]
    }
    $raw = [Convert]::FromBase64String($Text)
    if ($raw.Length -ne 32) { Stop-Install 'The release key must be 32 raw Ed25519 bytes.' }
    return [byte[]]$raw
}

function Get-KeyFingerprint([byte[]]$Key) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hex = ([BitConverter]::ToString($sha.ComputeHash($Key))).Replace('-', '')
    return (($hex -split '(.{4})' | Where-Object { $_ }) -join ' ')
}

function New-ServicePassword {
    # No XML or quoting characters: it goes into the WinSW file for one command.
    $upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    $lower = 'abcdefghijkmnopqrstuvwxyz'
    $digits = '23456789'
    $symbols = '-_.!#%+='
    $all = $upper + $lower + $digits + $symbols
    $bytes = New-Object byte[] 40
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $chars = New-Object System.Collections.Generic.List[char]
    for ($i = 0; $i -lt 28; $i++) { $chars.Add($all[$bytes[$i] % $all.Length]) }
    $chars.Add($upper[$bytes[28] % $upper.Length])
    $chars.Add($lower[$bytes[29] % $lower.Length])
    $chars.Add($digits[$bytes[30] % $digits.Length])
    $chars.Add($symbols[$bytes[31] % $symbols.Length])
    return (-join $chars)
}

function Set-FolderAcl([string]$Path, [string]$AgentRights) {
    # Owner-proof ACL: Administrators and SYSTEM full control, bdr-agent as given.
    # Reset the tree to inherited ACEs first, then protect only the folder itself; Windows pushes the
    # new ACEs down. Stripping inheritance per file (/T with /inheritance:r) left files the Python MSI
    # created (not owned by Administrators) with an empty DACL nobody could fix.
    $agent = "$env:COMPUTERNAME\$ServiceUser"
    & icacls.exe $Path /reset /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Could not reset permissions under $Path (icacls exit $LASTEXITCODE)." }
    & icacls.exe $Path /inheritance:r /grant:r "*${SidAdministrators}:(OI)(CI)F" "*${SidSystem}:(OI)(CI)F" "${agent}:(OI)(CI)$AgentRights" /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Could not set permissions on $Path (icacls exit $LASTEXITCODE)." }
}

function Set-FileAcl([string]$Path, [string]$AgentRights) {
    $agent = "$env:COMPUTERNAME\$ServiceUser"
    & icacls.exe $Path /inheritance:r /grant:r "*${SidAdministrators}:F" "*${SidSystem}:F" "${agent}:$AgentRights" /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Could not set permissions on $Path (icacls exit $LASTEXITCODE)." }
}

function Stop-AgentProcesses {
    $root = $InstallRoot.TrimEnd('\') + '\'
    Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
    } | ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { }
    }
}

function Stop-AgentService {
    $svc = Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Stopped') {
        Write-Host 'Stopping the BDR Agent service (running jobs are handed back to BDR)...'
        Stop-Service -Name $ServiceId -Force -ErrorAction SilentlyContinue
        try { $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(90)) } catch { }
    }
    Stop-AgentProcesses
}

function Invoke-Launcher([string[]]$Arguments) {
    $out = & $script:Python -I $script:LauncherPy --home $InstallRoot @Arguments
    return @{ Code = $LASTEXITCODE; Output = ($out -join "`n") }
}

function Resolve-MachinePython([string]$ExplicitPath) {
    # Read only the machine registration. py.exe can prefer a user-registered
    # interpreter, so never execute it in an elevated installer to discover Python.
    if ($ExplicitPath) {
        if (-not [IO.Path]::IsPathRooted($ExplicitPath) -or $ExplicitPath -notmatch '^[A-Za-z]:\\' -or $ExplicitPath.Substring(2).Contains(':')) {
            Stop-Install '-PythonPath must be an absolute local path to python.exe under Program Files.'
        }
        $found = $ExplicitPath
    } else {
        $key = 'HKLM:\SOFTWARE\Python\PythonCore\3.12\InstallPath'
        if (-not (Test-Path -LiteralPath $key)) {
            Stop-Install 'Prepare an administrator-managed Python 3.12 x64 under Program Files, then rerun with -PythonPath or -UseExistingPython.'
        }
        $registration = Get-Item -LiteralPath $key
        $found = $registration.GetValue('ExecutablePath', '')
        if (-not $found) { $found = Join-Path ($registration.GetValue('')) 'python.exe' }
    }
    $found = [IO.Path]::GetFullPath($found)
    $prefix = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\') + '\'
    if (-not $found.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $found -PathType Leaf)) {
        Stop-Install 'The shared Python runtime must be under Program Files. The other worker runtime will not be moved or copied.'
    }
    if ([IO.Path]::GetFileName($found) -ine 'python.exe') { Stop-Install 'The shared Python runtime executable must be named python.exe.' }
    $root = Split-Path -Parent $found
    $path = Get-Item -LiteralPath $root
    $ancestors = @()
    while ($path -and $path.FullName.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        if ($path.Attributes -band [IO.FileAttributes]::ReparsePoint) { Stop-Install 'The shared Python runtime path must not contain reparse points.' }
        $ancestors += $path
        $path = $path.Parent
    }
    $writeMask = [Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
    $trusted = @('S-1-5-18', 'S-1-5-32-544') # SYSTEM, Administrators
    $entries = $ancestors + @(Get-ChildItem -LiteralPath $root -Recurse -Force)
    foreach ($entry in $entries) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { Stop-Install 'The shared Python runtime must not contain reparse points.' }
        $acl = Get-Acl -LiteralPath $entry.FullName
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $trusted) { Stop-Install 'The shared Python runtime must be owned by Administrators or SYSTEM.' }
        foreach ($rule in $acl.Access) {
            if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
            if ($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $writeMask)) {
                $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
                if ($sid -notin $trusted) { Stop-Install 'The shared Python runtime must not be writable by non-administrators.' }
            }
        }
    }
    $signature = Get-AuthenticodeSignature -FilePath $found
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
        Stop-Install 'The shared Python executable must have a valid Python Software Foundation signature.'
    }
    $version = & $found -I -S -c 'import sys, sysconfig, venv, ensurepip; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor)+chr(124)+sysconfig.get_platform()); ensurepip.version()'
    if ($LASTEXITCODE -ne 0 -or $version -ne '3.12|win-amd64') { Stop-Install 'The shared Python runtime must be Python 3.12 x64 with venv and ensurepip.' }
    return $found
}

# 0. Preconditions ----------------------------------------------------------------------------

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Stop-Install 'Run this from an elevated PowerShell (right-click, Run as administrator).'
}
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    Stop-Install 'Use 64-bit Windows and 64-bit PowerShell.'
}
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop

if ($ApiBase) { $Api = $ApiBase.Trim().TrimEnd('/') }
elseif (Test-Filled $EmbeddedApiBase) { $Api = $EmbeddedApiBase }
else { Stop-Install 'No BDR API address. Pass -ApiBase https://<bdr-api>.' }
if (-not $Api.StartsWith('https://')) { Stop-Install 'The BDR API address must start with https://' }

$existingService = Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
if ($existingService -and -not $Reenroll) {
    Stop-Install "BDR Agent is already installed. To re-enroll it with a new code, run this again with -Reenroll. To remove it, copy $InstallRoot\service\uninstall.ps1 to another folder and run it there as Administrator."
}

# Resolve prerequisites before replacing launchers, service credentials or files.
$ExistingPython = $null
if ($PythonPath) { $UseExistingPython = $true }
if ($UseExistingPython) {
    $ExistingPython = Resolve-MachinePython -ExplicitPath $PythonPath
} elseif (-not (Test-Path -LiteralPath (Join-Path $InstallRoot 'python\python.exe')) -and
          (Test-Path -LiteralPath (Join-Path $otherRoot 'python\python.exe'))) {
    Stop-Install 'The other worker already owns a private Python 3.12 MSI runtime. Windows cannot install the same MSI into a second home. Prepare the official Python NuGet runtime under Program Files, then rerun with -PythonPath pointing to its python.exe. The existing worker was not changed.'
}

# 1. Release key fingerprint (nothing changes before the owner confirms) --------------------

Write-Step 'Release key check'
if (Test-Filled $EmbeddedPubKeyB64) {
    $KeyBytes = Get-RawKeyFromText $EmbeddedPubKeyB64
} else {
    $keyFile = Join-Path $PSScriptRoot 'release_key.pub'
    if (-not (Test-Path -LiteralPath $keyFile)) { Stop-Install "No release key in this script and none at $keyFile." }
    $KeyBytes = Get-RawKeyFromText (Get-Content -LiteralPath $keyFile -Raw)
}
$Fingerprint = Get-KeyFingerprint $KeyBytes
Write-Host ''
Write-Host 'Release public key fingerprint:' -ForegroundColor Yellow
Write-Host "    $Fingerprint" -ForegroundColor Yellow
Write-Host ''
Write-Host 'Open the password manager entry "BDR agent release key fingerprint" and compare'
Write-Host 'every group. If even one character differs, do NOT continue: tell IT Admin.'
$answer = Read-Host 'Type yes if the fingerprint matches exactly'
if ($answer.Trim().ToLowerInvariant() -ne 'yes') { Stop-Install 'Fingerprint not confirmed. Nothing was changed.' }

# 2. Enrollment code -----------------------------------------------------------------------------

if (-not $Code) { $Code = Read-Host 'Enrollment code from BDR (20 characters)' }
$Code = (($Code.ToUpperInvariant()).ToCharArray() | Where-Object { [char]::IsLetterOrDigit($_) }) -join ''
if ($Code -notmatch '^[A-Z2-7]{20}$') { Stop-Install 'The enrollment code must be 20 characters (letters A-Z and digits 2-7). Ask IT Admin for a new one.' }

# 3. Release bundle (first, because a rendered script's download link lasts 15 minutes) ---------

Write-Step 'Getting the worker release'
$Work = Join-Path ([IO.Path]::GetTempPath()) ('bdr-agent-install-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Work -Force | Out-Null
$BundleZip = $null
$ReleaseJson = $null
if ($BundlePath) {
    if (-not $ReleaseJsonPath) { Stop-Install '-BundlePath needs -ReleaseJsonPath.' }
    $BundleZip = Join-Path $Work 'bundle.zip'
    Copy-Item -LiteralPath $BundlePath -Destination $BundleZip
    $ReleaseJson = Join-Path $Work 'release.json'
    Copy-Item -LiteralPath $ReleaseJsonPath -Destination $ReleaseJson
} elseif (Test-Filled $EmbeddedReleaseB64) {
    $releaseText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EmbeddedReleaseB64))
    $releaseObj = $releaseText | ConvertFrom-Json
    $ReleaseJson = Join-Path $Work 'release.json'
    # Written byte for byte: re-serializing could change the signed text.
    [IO.File]::WriteAllText($ReleaseJson, $releaseText, (New-Object Text.UTF8Encoding($false)))
    $BundleZip = Join-Path $Work 'bundle.zip'
    try { Invoke-Download $releaseObj.url $BundleZip }
    catch { Stop-Install 'Could not download the release. The link in this installer lasts 15 minutes: download the installer again from BDR and run it right away.' }
} elseif (-not $Reenroll) {
    Stop-Install 'This installer carries no release. Download a fresh installer from BDR, or pass -BundlePath and -ReleaseJsonPath.'
}
if ($BundleZip) {
    $releaseManifest = ((Get-Content -LiteralPath $ReleaseJson -Raw) | ConvertFrom-Json).manifest
    if ($Pool -eq 'user' -and [Version]$releaseManifest.version -lt [Version]'1.0.1') {
        Stop-Install 'Personal workers require release 1.0.1 or later. Ask IT Admin to publish and select the personal-worker release, then download a fresh installer.'
    }
    $manifestSha = $releaseManifest.sha256
    if ((Get-Sha256 $BundleZip) -ne $manifestSha) { Stop-Install 'The downloaded release does not match its manifest (sha256).' }
    Write-Host 'Release downloaded; sha256 matches the manifest.'
}

if ($Reenroll) {
    Write-Step 'Stopping the running worker'
    Stop-AgentService
}

# 4. Folders and launcher ---------------------------------------------------------------------

Write-Step "Preparing $InstallRoot"
foreach ($d in @('', 'versions', 'state', 'state\health', 'logs', 'logs\service', 'spool', 'tmp', 'ms-playwright', 'service')) {
    New-Item -ItemType Directory -Path (Join-Path $InstallRoot $d) -Force | Out-Null
}
$LauncherDir = Join-Path $InstallRoot 'launcher'
if (Test-Path -LiteralPath $LauncherDir) { Remove-Item -LiteralPath $LauncherDir -Recurse -Force }
New-Item -ItemType Directory -Path $LauncherDir -Force | Out-Null
if (Test-Filled $EmbeddedLauncherB64) {
    $launcherZip = Join-Path $Work 'launcher.zip'
    [IO.File]::WriteAllBytes($launcherZip, [Convert]::FromBase64String($EmbeddedLauncherB64))
    Expand-Archive -LiteralPath $launcherZip -DestinationPath $LauncherDir -Force
} else {
    $src = Join-Path (Split-Path -Parent $PSScriptRoot) 'launcher'
    if (-not (Test-Path -LiteralPath (Join-Path $src 'launcher.py'))) { Stop-Install "Launcher files not found at $src." }
    Copy-Item -Path (Join-Path $src 'launcher.py') -Destination $LauncherDir
    New-Item -ItemType Directory -Path (Join-Path $LauncherDir 'bdr_launcher') -Force | Out-Null
    Copy-Item -Path (Join-Path $src 'bdr_launcher\*.py') -Destination (Join-Path $LauncherDir 'bdr_launcher')
}
# Keep a copy of the uninstaller on the PC (admin-only service folder): the
# rendered installer carries it inside the launcher zip.
$UninstallDest = Join-Path $InstallRoot 'service\uninstall.ps1'
$UninstallSrc = Join-Path $LauncherDir 'uninstall.ps1'
if (Test-Path -LiteralPath $UninstallSrc) {
    Move-Item -LiteralPath $UninstallSrc -Destination $UninstallDest -Force
} elseif ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'uninstall.ps1'))) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall.ps1') -Destination $UninstallDest -Force
}
$script:LauncherPy = Join-Path $LauncherDir 'launcher.py'
$keyB64 = [Convert]::ToBase64String($KeyBytes)
$KeyPath = Join-Path $LauncherDir 'release_key.pub'
[IO.File]::WriteAllText($KeyPath, $keyB64 + "`n", (New-Object Text.UTF8Encoding($false)))
$WorkerKeyPath = Join-Path $InstallRoot 'release_key.pub'
if (Test-Path -LiteralPath $WorkerKeyPath) { Remove-Item -LiteralPath $WorkerKeyPath -Force }
[IO.File]::WriteAllText($WorkerKeyPath, $keyB64 + "`n", (New-Object Text.UTF8Encoding($false)))

# 5. Python runtime ---------------------------------------------------------------------------

Write-Step "Python $PythonVersion runtime"
$PythonDir = Join-Path $InstallRoot 'python'
$script:Python = Join-Path $PythonDir 'python.exe'
$pythonInstalledByUs = $false
if ($UseExistingPython) {
    $script:Python = $ExistingPython
} elseif (Test-Path -LiteralPath $script:Python) {
    $ver = & $script:Python -I -c 'import sys; print(*sys.version_info[:2], sep=chr(46))'
    if ($ver -ne '3.12') { Stop-Install "$script:Python is Python $ver, expected 3.12. Run $InstallRoot\service\uninstall.ps1 first (copy it to another folder, then run it)." }
    Write-Host "Using the existing runtime at $PythonDir"
} else {
    $cache = Join-Path $InstallRoot 'python-installer'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $pyInstaller = Join-Path $cache "python-$PythonVersion-amd64.exe"
    Write-Host "Downloading $PythonUrl"
    Invoke-Download $PythonUrl $pyInstaller
    if ((Get-Sha256 $pyInstaller) -ne $PythonSha256) { Stop-Install 'The Python installer does not match its pinned sha256.' }
    $sig = Get-AuthenticodeSignature -FilePath $pyInstaller
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
        Stop-Install 'The Python installer is not signed by the Python Software Foundation.'
    }
    $pyArgs = @('/quiet', 'InstallAllUsers=1', "TargetDir=`"$PythonDir`"", 'Include_launcher=0', 'InstallLauncherAllUsers=0',
                'Include_test=0', 'Include_doc=0', 'Include_tcltk=0', 'Include_dev=0', 'Include_pip=1',
                'PrependPath=0', 'Shortcuts=0', 'AssociateFiles=0', 'CompileAll=0')
    $p = Start-Process -FilePath $pyInstaller -ArgumentList $pyArgs -Wait -PassThru
    if ($p.ExitCode -ne 0) { Stop-Install "The Python installer failed (exit $($p.ExitCode))." }
    if (-not (Test-Path -LiteralPath $script:Python)) {
        Stop-Install "Python $PythonVersion seems to be installed for all users elsewhere already, so the installer did not create $PythonDir. Run again with -UseExistingPython."
    }
    $pythonInstalledByUs = $true
}

# 6. Service user -------------------------------------------------------------------------------

Write-Step "Service user $ServiceUser"
$Password = New-ServicePassword
$SecurePassword = ConvertTo-SecureString $Password -AsPlainText -Force
$user = Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue
if ($user) {
    # A reset makes the old DPAPI token unreadable; that is why -Reenroll needs a new code.
    Set-LocalUser -Name $ServiceUser -Password $SecurePassword -PasswordNeverExpires $true
    Enable-LocalUser -Name $ServiceUser
    Write-Host 'Existing user: password reset.'
} else {
    New-LocalUser -Name $ServiceUser -Password $SecurePassword -PasswordNeverExpires -UserMayNotChangePassword `
        -AccountNeverExpires -FullName 'BDR Agent' -Description 'Runs the BDR Agent service. Do not sign in.' | Out-Null
    Write-Host 'User created.'
}
$usersGroup = Get-LocalGroup -SID $SidUsers
try { Add-LocalGroupMember -Group $usersGroup -Member $ServiceUser -ErrorAction Stop }
catch [Microsoft.PowerShell.Commands.MemberExistsException] { }
$hide = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
New-Item -Path $hide -Force | Out-Null
New-ItemProperty -Path $hide -Name $ServiceUser -Value 0 -PropertyType DWord -Force | Out-Null
$Credential = New-Object System.Management.Automation.PSCredential("$env:COMPUTERNAME\$ServiceUser", $SecurePassword)

# 7. Permissions ---------------------------------------------------------------------------------

Write-Step 'Permissions'
Set-FolderAcl $InstallRoot 'M'
foreach ($locked in @($LauncherDir, (Join-Path $InstallRoot 'service'))) { Set-FolderAcl $locked 'RX' }
if (Test-Path -LiteralPath $PythonDir) { Set-FolderAcl $PythonDir 'RX' }
if (Test-Path -LiteralPath (Join-Path $InstallRoot 'python-installer')) { Set-FolderAcl (Join-Path $InstallRoot 'python-installer') 'RX' }
Set-FileAcl $WorkerKeyPath 'R'
Write-Host 'bdr-agent can write its data folders; launcher, runtime, service files and the release key are read-only to it.'

# 8. Install the signed release side by side -------------------------------------------------------

if ($BundleZip) {
    Write-Step 'Verifying and installing the worker release (this downloads Chromium, a few minutes)'
    $r = Invoke-Launcher @('install-bundle', '--zip', $BundleZip, '--release-json', $ReleaseJson)
    Write-Host $r.Output
    if ($r.Code -eq 10) { Stop-Install 'The release was REJECTED (signature, sha256 or version floor). Do not retry; tell IT Admin.' }
    if ($r.Code -ne 0) { Stop-Install "Installing the release failed. See $InstallRoot\logs\launcher.log" }
}
$r = Invoke-Launcher @('status', '--python-path')
if ($r.Code -ne 0) { Stop-Install 'No worker version is installed. Download a fresh installer from BDR.' }
$WorkerPython = $r.Output.Trim()

# 9. Enroll as bdr-agent -------------------------------------------------------------------------

Write-Step 'Enrolling this PC with BDR'
$enrollOut = Join-Path $InstallRoot 'logs\enroll.out.txt'
$enrollErr = Join-Path $InstallRoot 'logs\enroll.err.txt'
$fpHex = $Fingerprint.Replace(' ', '')
$enrollArgs = @('-I', '-X', 'utf8', '-m', 'bdr_agent', '--home', "`"$InstallRoot`"", 'enroll', '--api', $Api,
                '--code', $Code, '--pool', $Pool, '--fingerprint', $fpHex, '--yes', '--release-key', "`"$WorkerKeyPath`"")
if ($Reenroll) { $enrollArgs += '--force' }
$p = Start-Process -FilePath $WorkerPython -ArgumentList $enrollArgs -Credential $Credential -LoadUserProfile `
    -WorkingDirectory $InstallRoot -Wait -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $enrollOut -RedirectStandardError $enrollErr
if (Test-Path -LiteralPath $enrollOut) { Get-Content -LiteralPath $enrollOut | Write-Host }
if ($p.ExitCode -ne 0) {
    if (Test-Path -LiteralPath $enrollErr) { Get-Content -LiteralPath $enrollErr | Write-Host -ForegroundColor Red }
    Stop-Install 'Enrollment failed. If the code expired (30 minutes) or was used, ask IT Admin for a new one and run this again with -Reenroll.'
}

# 10. Windows service ------------------------------------------------------------------------------

Write-Step 'Registering the BDR Agent service'
$ServiceDir = Join-Path $InstallRoot 'service'
$Winsw = Join-Path $ServiceDir 'BDRAgent.exe'
$WinswXml = Join-Path $ServiceDir 'BDRAgent.xml'
if (-not (Test-Path -LiteralPath $Winsw) -or (Get-Sha256 $Winsw) -ne $WinswSha256) {
    $dl = Join-Path $Work 'WinSW-x64.exe'
    Invoke-Download $WinswUrl $dl
    if ((Get-Sha256 $dl) -ne $WinswSha256) { Stop-Install 'WinSW does not match its pinned sha256.' }
    Copy-Item -LiteralPath $dl -Destination $Winsw -Force
}
if (Test-Filled $EmbeddedWinswXmlB64) {
    $template = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EmbeddedWinswXmlB64))
} else {
    $template = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'winsw\BDRAgent.xml') -Raw
}
$template = $template.Replace('<id>BDRAgent</id>', '<id>' + $ServiceId + '</id>').Replace('<name>BDR Agent</name>', '<name>BDR Agent ' + $Pool + '</name>')
$xml = $template.Replace('{{PYTHON}}', [Security.SecurityElement]::Escape($script:Python)).
    Replace('{{HOME}}', [Security.SecurityElement]::Escape($InstallRoot)).
    Replace('{{SERVICE_USER}}', $ServiceUser).
    Replace('{{SERVICE_PASSWORD}}', [Security.SecurityElement]::Escape($Password))
[IO.File]::WriteAllText($WinswXml, $xml, (New-Object Text.UTF8Encoding($false)))
if (Get-Service -Name $ServiceId -ErrorAction SilentlyContinue) {
    & $Winsw uninstall | Out-Null
    for ($i = 0; $i -lt 30 -and (Get-Service -Name $ServiceId -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
}
& $Winsw install
$installCode = $LASTEXITCODE
# The password was only needed by that one command: remove it from disk now.
$scrubbed = [Regex]::Replace($xml, '(?s)<!-- SERVICEACCOUNT-BEGIN -->.*?<!-- SERVICEACCOUNT-END -->',
    '<!-- Service account set at install time (bdr-agent). The password is not stored. -->')
[IO.File]::WriteAllText($WinswXml, $scrubbed, (New-Object Text.UTF8Encoding($false)))
$Password = $null
if ($installCode -ne 0) { Stop-Install "WinSW could not register the service (exit $installCode)." }

$installInfo = @{ python = $script:Python; python_installed_by_us = $pythonInstalledByUs; python_version = $PythonVersion;
                  installed_at = (Get-Date).ToUniversalTime().ToString('o'); api = $Api; pool = $Pool } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $ServiceDir 'install.json'), $installInfo, (New-Object Text.UTF8Encoding($false)))

Start-Service -Name $ServiceId
Write-Host 'Service started. Waiting for the worker to report healthy (up to 4 minutes)...'
$launcherLog = Join-Path $InstallRoot 'logs\launcher.log'
$healthy = $false
for ($i = 0; $i -lt 48; $i++) {
    Start-Sleep -Seconds 5
    if ((Test-Path -LiteralPath $launcherLog) -and (Select-String -LiteralPath $launcherLog -Pattern 'is healthy' -SimpleMatch -Quiet)) {
        $healthy = $true
        break
    }
}
Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
if ($healthy) {
    Write-Host 'DONE. The worker is running on this PC.' -ForegroundColor Green
} else {
    Write-Host "The service is installed but has not reported healthy yet. Check $launcherLog and $InstallRoot\logs\worker.log" -ForegroundColor Yellow
}
Write-Host 'Next: ask IT Admin to click Approve for this PC in BDR Settings > Browser agents.'
if ($Pool -eq 'user') {
    Write-Host 'Allow Personal browser read for this personal worker. Run self-test is for organization workers only.'
    Write-Host 'Complete personal browser setup in BDR before requesting a read from the assistant.'
} else {
    Write-Host 'Enable the Worker self-test type and allow it for this organization worker.'
    Write-Host 'Click Run self-test once the worker shows Online.'
}
Write-Host "If it stays Offline, check the $ServiceId service and $InstallRoot\logs\worker.log for connection errors."
