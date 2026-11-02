<#
.SYNOPSIS
    Installs the official CPython NuGet runtime beside the existing BDR worker.
.DESCRIPTION
    Run in elevated 64-bit Windows PowerShell. Uses a complete archive hash
    pinned by the Mac preparation tool after checking NuGet catalog metadata.
    Does not change services, PATH, Python registry entries or existing Python.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$ExpectedSha256 = '__PYTHON_NUGET_SHA256__'
$PackageUrl = 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or
    -not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw 'Run this in 64-bit Windows PowerShell as Administrator.'
}
if ($ExpectedSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Download a prepared runtime setup script from BDR.' }

$programs = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
if ((Get-Item -LiteralPath $programs).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Program Files must not be a reparse point.'
}
$destination = Join-Path $programs 'BDR Python 3.12.10'
if (Test-Path -LiteralPath $destination) {
    Write-Host "Runtime folder already exists: $destination"
    Write-Host 'No files changed. The BDR installer will verify this runtime before using it.'
    exit 0
}
$stage = Join-Path $programs ('BDR Python Setup-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    # Protect the download and extraction before writing any executable files.
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $acl.SetOwner($admins)
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($sid in @('S-1-5-32-544', 'S-1-5-18', 'S-1-5-32-545')) {
        $rights = if ($sid -eq 'S-1-5-32-545') { 'ReadAndExecute' } else { 'FullControl' }
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, $rights, $inherit, 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $stage -AclObject $acl

    Write-Host 'Downloading the official Python 3.12.10 x64 package...'
    $archive = Join-Path $stage 'python.nupkg'
    Invoke-WebRequest -UseBasicParsing -Uri $PackageUrl -OutFile $archive -TimeoutSec 180 -MaximumRedirection 3
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $ExpectedSha256) {
        throw 'Python package hash mismatch. Nothing was installed.'
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $unpack = Join-Path $stage 'package'
    $prefix = [IO.Path]::GetFullPath($unpack).TrimEnd('\') + '\'
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        if ($zip.Entries.Count -gt 20000) { throw 'Python package has too many entries.' }
        [long]$total = 0
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $unpack $entry.FullName))
            $unixKind = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($entry.FullName.Contains(':') -or [IO.Path]::IsPathRooted($entry.FullName) -or
                -not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
                -not $names.Add($target) -or $unixKind -eq 0xA000 -or
                ($entry.ExternalAttributes -band [int][IO.FileAttributes]::ReparsePoint)) {
                throw 'Python package contains an unsafe archive entry.'
            }
            $total += $entry.Length
            if ($total -gt 300MB) { throw 'Python package exceeds the extraction limit.' }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $unpack)

    $python = Join-Path $unpack 'tools\python.exe'
    foreach ($name in @('tools\python.exe', 'tools\python312.dll')) {
        $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $unpack $name)
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
            throw 'Python executable signature verification failed.'
        }
    }
    # The full archive is authenticated before Python or any stdlib code runs.
    & $python -I -S -c 'import sys, struct, venv, ensurepip; assert sys.version_info[:3] == (3,12,10) and struct.calcsize(chr(80)) == 8'
    if ($LASTEXITCODE -ne 0) { throw 'Python version, architecture or required modules are incompatible.' }
    $smoke = Join-Path $stage 'verify-venv'
    & $python -I -m venv $smoke
    if ($LASTEXITCODE -ne 0) { throw 'Python could not create a worker virtual environment.' }
    & (Join-Path $smoke 'Scripts\python.exe') -I -m pip --version
    if ($LASTEXITCODE -ne 0) { throw 'The worker virtual environment has no working pip.' }

    # Keep the protected runtime ACL when moving it to its final parent.
    $runtimeAcl = Get-Acl -LiteralPath $unpack
    $runtimeAcl.SetAccessRuleProtection($true, $true)
    $runtimeAcl.SetOwner($admins)
    Set-Acl -LiteralPath $unpack -AclObject $runtimeAcl
    & icacls.exe $unpack /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not assign administrator ownership to the runtime.' }
    if (Test-Path -LiteralPath $destination) { throw 'The destination appeared during setup; no existing files were replaced.' }
    Move-Item -LiteralPath $unpack -Destination $destination
    Write-Host 'Personal worker Python runtime is ready.' -ForegroundColor Green
    Write-Host (Join-Path $destination 'tools\python.exe')
    Write-Host 'Existing Python registration and the organization worker were not changed.'
} finally {
    if (Test-Path -LiteralPath $stage) {
        try { Remove-Item -LiteralPath $stage -Recurse -Force }
        catch { Write-Warning "Could not remove protected staging folder: $stage" }
    }
}
