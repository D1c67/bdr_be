<#
.SYNOPSIS
    Removes the BDR browser agent worker from this PC.

.DESCRIPTION
    Run as Administrator:

        powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

    Stops and deletes the "BDR Agent" service, stops any leftover worker or
    Chromium processes, deletes the bdr-agent user and its profile, uninstalls
    the private Python runtime (when the installer added it) and deletes
    C:\ProgramData\BDRAgent.

    Also click Revoke for this PC in BDR Settings > Browser agents: deleting the
    files does not cancel the worker token on the BDR side.

.PARAMETER KeepData
    Keep C:\ProgramData\BDRAgent\logs (for support). Everything else is removed.

.PARAMETER KeepPython
    Leave the private Python runtime in place.
#>
[CmdletBinding()]
param(
    [ValidateSet('org', 'user')]
    [string]$Pool = 'org',
    [string]$InstallRoot = (Join-Path $env:ProgramData 'BDRAgent'),
    [switch]$KeepData,
    [switch]$KeepPython
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0

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


$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this from an elevated PowerShell (right-click, Run as administrator).' -ForegroundColor Red
    exit 1
}
Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop

$answer = Read-Host 'This removes the BDR Agent service and its files from this PC. Type remove to continue'
if ($answer.Trim().ToLowerInvariant() -ne 'remove') { Write-Host 'Nothing was changed.'; exit 1 }

Write-Host '==> Stopping the service' -ForegroundColor Cyan
$svc = Get-Service -Name $ServiceId -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceId -Force -ErrorAction SilentlyContinue
        try { $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(90)) } catch { }
    }
    $winsw = Join-Path $InstallRoot 'service\BDRAgent.exe'
    if (Test-Path -LiteralPath $winsw) { & $winsw uninstall | Out-Null }
    if (Get-Service -Name $ServiceId -ErrorAction SilentlyContinue) { & sc.exe delete $ServiceId | Out-Null }
}

$root = $InstallRoot.TrimEnd('\') + '\'
Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
} | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { } }

Write-Host '==> Removing the bdr-agent user' -ForegroundColor Cyan
$user = Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue
if ($user) {
    $sid = $user.SID.Value
    Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid } | ForEach-Object {
        try { Remove-CimInstance -InputObject $_ } catch { Write-Host "Could not delete the profile folder $($_.LocalPath); delete it later." }
    }
    Remove-LocalUser -Name $ServiceUser
}
$hide = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
if (Test-Path $hide) { Remove-ItemProperty -Path $hide -Name $ServiceUser -ErrorAction SilentlyContinue }

$infoPath = Join-Path $InstallRoot 'service\install.json'
if (-not $KeepPython -and (Test-Path -LiteralPath $infoPath)) {
    $info = Get-Content -LiteralPath $infoPath -Raw | ConvertFrom-Json
    if ($info.python_installed_by_us) {
        Write-Host '==> Uninstalling the private Python runtime' -ForegroundColor Cyan
        $installer = Join-Path $InstallRoot ("python-installer\python-" + $info.python_version + "-amd64.exe")
        if (Test-Path -LiteralPath $installer) {
            $p = Start-Process -FilePath $installer -ArgumentList @('/uninstall', '/quiet') -Wait -PassThru
            if ($p.ExitCode -ne 0) { Write-Host "The Python uninstaller returned $($p.ExitCode); the folder is deleted anyway." }
        }
    }
}

Write-Host "==> Deleting $InstallRoot" -ForegroundColor Cyan
if (Test-Path -LiteralPath $InstallRoot) {
    & icacls.exe $InstallRoot /reset /T /C /Q | Out-Null
    Get-ChildItem -LiteralPath $InstallRoot -Force | Where-Object {
        -not ($KeepData -and $_.Name -eq 'logs')
    } | Remove-Item -Recurse -Force -ErrorAction Continue
    if (-not $KeepData) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction Continue }
}

Write-Host ''
Write-Host 'BDR Agent is removed from this PC.' -ForegroundColor Green
Write-Host 'Now click Revoke for this PC in BDR Settings > Browser agents, if not done already.'
