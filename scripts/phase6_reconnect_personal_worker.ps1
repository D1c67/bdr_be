# Run in elevated Windows PowerShell. This reconnects only the enrolled personal worker.
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ApiBase, [Parameter(Mandatory = $true)][string]$ExpectedWorkerId)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
$ProgressPreference = 'SilentlyContinue'
$ServiceName = 'BDRAgentUser'
$WorkerHome = 'C:\ProgramData\BDRAgentUser'
$ConfigPath = Join-Path $WorkerHome 'worker.json'
$ServiceDir = Join-Path $WorkerHome 'service'

function Assert-PlainPath([string]$Path) {
    $entry = Get-Item -LiteralPath $Path -Force
    while ($entry) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Refusing a worker path containing a reparse point.'
        }
        if ($entry -is [IO.FileInfo]) { $entry = $entry.Directory } else { $entry = $entry.Parent }
    }
}

function Read-WorkerConfig([byte[]]$Bytes) {
    if ($Bytes.Length -gt 65536) { throw 'Worker configuration is unexpectedly large.' }
    $config = [Text.Encoding]::UTF8.GetString($Bytes).TrimStart([char]0xFEFF) | ConvertFrom-Json
    if (-not $config -or -not $config.PSObject.Properties['worker_id'] -or
        -not $config.PSObject.Properties['api_base_url'] -or
        $config.worker_id -cne $ExpectedWorkerId) {
        throw 'This is not the expected enrolled personal worker.'
    }
    return $config
}

function Wait-PersonalService([string]$Status, [int]$Seconds) {
    $service = Get-Service -Name $ServiceName
    $target = [Enum]::Parse([ServiceProcess.ServiceControllerStatus], $Status)
    $service.WaitForStatus($target, [TimeSpan]::FromSeconds($Seconds))
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this block in an elevated PowerShell window.'
}
# The id comes from the Mac helper (the one approved personal worker row); the
# 1.1.0 rollout re-enrolls the service, which gives it a new row.
if ($ExpectedWorkerId -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
    throw 'ExpectedWorkerId must be the lowercase worker UUID printed by the Mac helper.'
}
if ($ApiBase -cnotmatch '^https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com/?$') {
    throw 'ApiBase must be an exact https trycloudflare.com tunnel origin, without credentials, port, query or path.'
}
$ApiBase = $ApiBase.TrimEnd('/')
foreach ($path in @($ConfigPath, $ServiceDir, (Join-Path $ServiceDir 'install.json'), (Join-Path $ServiceDir 'BDRAgent.xml'))) {
    Assert-PlainPath $path
}
$install = Get-Content -LiteralPath (Join-Path $ServiceDir 'install.json') -Raw | ConvertFrom-Json
if (-not $install.PSObject.Properties['pool'] -or $install.pool -cne 'user') {
    throw 'The installation metadata does not identify the personal pool.'
}
$serviceInfo = Get-CimInstance Win32_Service -Filter "Name='BDRAgentUser'"
$expectedExe = Join-Path $ServiceDir 'BDRAgent.exe'
if (-not $serviceInfo -or $serviceInfo.PathName.Trim('"') -ine $expectedExe -or
    @('.\bdr-agent-user', ($env:COMPUTERNAME + '\bdr-agent-user')) -inotcontains $serviceInfo.StartName) {
    throw 'The personal service executable or account does not match this installation.'
}
Assert-PlainPath $expectedExe
[xml]$serviceXml = Get-Content -LiteralPath (Join-Path $ServiceDir 'BDRAgent.xml') -Raw
if ($serviceXml.service.id -cne $ServiceName -or $serviceXml.service.workingdirectory -ine $WorkerHome) {
    throw 'The service definition does not match the personal worker.'
}
if ((Get-Service -Name $ServiceName).Status -ne 'Running') {
    throw 'The personal service must be running before this reconnect operation.'
}
if ((Get-Item -LiteralPath $ConfigPath).Length -gt 65536) { throw 'Worker configuration is unexpectedly large.' }
$null = Read-WorkerConfig ([IO.File]::ReadAllBytes($ConfigPath))
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$health = Invoke-WebRequest -UseBasicParsing -Uri ($ApiBase + '/health') -MaximumRedirection 0 -TimeoutSec 20
if ($health.StatusCode -ne 200 -or ($health.Content | ConvertFrom-Json).status -cne 'ok') {
    throw 'The new API health check failed. No service or configuration was changed.'
}

# Store only worker.json (never token.bin) in a private administrator-owned directory.
$backupDir = Join-Path $ServiceDir ('reconnect-' + [Guid]::NewGuid().ToString('N'))
$backupAcl = New-Object Security.AccessControl.DirectorySecurity
$backupAcl.SetAccessRuleProtection($true, $false)
$admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
$backupAcl.SetOwner($admins)
foreach ($sid in @('S-1-5-32-544', 'S-1-5-18')) {
    $identity = New-Object Security.Principal.SecurityIdentifier($sid)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $backupAcl.AddAccessRule($rule)
}
New-Item -ItemType Directory -Path $backupDir | Out-Null
Set-Acl -LiteralPath $backupDir -AclObject $backupAcl
$backupPath = Join-Path $backupDir 'worker.json.original'
$originalBytes = $null
$stream = $null
$stopped = $false
$writeStarted = $false
try {
    $stopped = $true
    Stop-Service -Name $ServiceName -ErrorAction Stop
    Wait-PersonalService 'Stopped' 90
    Assert-PlainPath $ConfigPath
    # Updating the existing file preserves its owner and ACL. An exclusive handle
    # prevents the worker or another process from changing it during the write.
    $stream = [IO.File]::Open($ConfigPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    if ($stream.Length -gt 65536) { throw 'Worker configuration is unexpectedly large.' }
    $originalBytes = New-Object byte[] ([int]$stream.Length)
    $offset = 0
    while ($offset -lt $originalBytes.Length) {
        $count = $stream.Read($originalBytes, $offset, $originalBytes.Length - $offset)
        if ($count -eq 0) { throw 'Could not read the complete worker configuration.' }
        $offset += $count
    }
    $config = Read-WorkerConfig $originalBytes
    [IO.File]::WriteAllBytes($backupPath, $originalBytes)
    $config.api_base_url = $ApiBase
    $updatedBytes = (New-Object Text.UTF8Encoding($false)).GetBytes(($config | ConvertTo-Json -Depth 32))
    $writeStarted = $true
    $stream.Position = 0
    $stream.Write($updatedBytes, 0, $updatedBytes.Length)
    $stream.SetLength($updatedBytes.Length)
    $stream.Flush($true)
    $stream.Dispose()
    $stream = $null
    Start-Service -Name $ServiceName -ErrorAction Stop
    Wait-PersonalService 'Running' 45
    Write-Host 'Personal worker API updated and BDRAgentUser restarted. Check its new heartbeat in BDR.'
    Write-Host ('Protected configuration backup: ' + $backupPath)
} catch {
    if ($stream) { $stream.Dispose(); $stream = $null }
    $restored = $false
    try {
        if ($stopped) {
            if ((Get-Service -Name $ServiceName).Status -ne 'Stopped') {
                Stop-Service -Name $ServiceName -ErrorAction Stop
                Wait-PersonalService 'Stopped' 90
            }
            if ($writeStarted -and $null -ne $originalBytes) {
                Assert-PlainPath $ConfigPath
                $stream = [IO.File]::Open($ConfigPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                $stream.Write($originalBytes, 0, $originalBytes.Length)
                $stream.SetLength($originalBytes.Length)
                $stream.Flush($true)
                $stream.Dispose()
                $stream = $null
            }
            Start-Service -Name $ServiceName -ErrorAction Stop
            Wait-PersonalService 'Running' 45
        }
        $restored = $true
    } catch {
        Write-Warning ('Automatic recovery failed. The protected configuration backup is ' + $backupPath)
    } finally {
        if ($stream) { $stream.Dispose(); $stream = $null }
    }
    if ($restored) { throw 'Reconnect failed; the original configuration and personal service state were restored.' }
    throw 'Reconnect failed and needs manual personal-service recovery. No org service was changed.'
}
