<#
.SYNOPSIS
    Blocks the BDR personal (user pool) browser worker from the office network.

.DESCRIPTION
    Run once, as Administrator, on the PC that runs the BDRAgentUser service
    (docs/ASSISTANT_BROWSER_V2_PLAN.md 10.1 layer 2, release 1.1.0 step 5):

        powershell -ExecutionPolicy Bypass -File .\apply_user_pool_firewall.ps1

    What it does:
      1. Adds Windows Firewall rules (group "BDR user pool floor"): Outbound,
         Block, every profile, for each program the user pool runs, to every
         private, link-local, CGNAT, multicast, reserved and documentation
         range (IPv4 and IPv6, including the NAT64 and 6to4 forms of the
         private IPv4 ranges) and to the LocalSubnet keyword. Loopback
         stays open so the processes reach their local egress proxy, which
         refuses loopback targets itself.
         Programs: the base python.exe of every installed worker version (a
         venv python.exe only redirects to it), and the stable copies the
         launcher keeps at <InstallRoot>\runtime\current\driver\node.exe and
         <InstallRoot>\runtime\current\chrome\chrome.exe. These paths do not
         change between releases or Playwright updates.
      2. Adds one more rule scoped to the service account's SID (all its
         processes). Whether Windows applies it to every child process is
         checked live; the program rules above do not depend on it.
      3. Finds the default gateway and the ports it serves (80, 443, 53) and
         writes <InstallRoot>\firewall\floor.json, which the worker's boot
         self-test reads: it must see the policy error WSAEACCES (10013) when
         python.exe, node.exe and Chromium try to reach that gateway.
      4. Creates the download quarantine C:\ProgramData\BDRQuarantine (outside
         every worker folder) and the jobs and runtime folders, with the
         service account allowed to write them. Only an interactive run does
         this (Administrators take ownership; a junction or link anywhere on
         the path stops the script); the SYSTEM re-apply task never touches
         folder permissions.
      5. Office-owned public addresses: Python's notion of "global" (the
         proxy's rule) and the ranges above both allow the office's own WAN
         IPv4, which hairpin NAT loops back into the LAN (port forwards, the
         router admin page), and the routed IPv6 prefixes of other office
         VLANs. The script blocks, in the same program rules, every range
         given with -OfficeBlockedRanges, the office WAN IPv4 it detects
         (myip.opendns.com), and the /56 of every global IPv6 address this PC
         got from a router advertisement. It records them in floor.json as
         extra_blocked; the worker's self-test requires them in the rules and
         every egress proxy refuses them.
      6. Warns when a Microsoft Defender exclusion covers the quarantine.
      7. Registers the SYSTEM scheduled task "BDR Agent User Firewall" that runs
         this script again (-Reapply) at startup and every 15 minutes, from an
         administrator-only copy in <InstallRoot>\firewall. A re-apply changes
         nothing when the rules are already right.

    Undo (rollback runbook): -Remove deletes the rules and the task and leaves
    the folders.

.PARAMETER InstallRoot
    The user pool home. Default C:\ProgramData\BDRAgentUser.

.PARAMETER QuarantineRoot
    Where downloads wait for the Defender scan. Default C:\ProgramData\BDRQuarantine.
    Must be under ProgramData. The service account may add and delete job
    folders inside it, never the folder itself.

.PARAMETER ServiceUser
    The user pool service account. Default bdr-agent-user.

.PARAMETER OfficeBlockedRanges
    Office-owned public addresses to block as well, for example the office
    WAN IPv4 (203.0.113.7/32) and the site IPv6 prefix (2001:db8:1200::/48).
    Remembered in floor.json for the scheduled re-apply.

.PARAMETER NoWanDetect
    Do not look up the office WAN IPv4 (myip.opendns.com).

.PARAMETER Reapply
    Non-interactive run (the scheduled task). Does not register the task again.

.PARAMETER NoScheduledTask
    Apply the rules but do not register the scheduled task.

.PARAMETER NoUserRule
    Skip the extra rule scoped to the service account SID.

.PARAMETER Remove
    Remove the rules and the scheduled task.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:ProgramData 'BDRAgentUser'),
    [string]$QuarantineRoot = (Join-Path $env:ProgramData 'BDRQuarantine'),
    [string]$ServiceUser = 'bdr-agent-user',
    [string[]]$OfficeBlockedRanges = @(),
    [switch]$NoWanDetect,
    [switch]$Reapply,
    [switch]$NoScheduledTask,
    [switch]$NoUserRule,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
$ProgressPreference = 'SilentlyContinue'

$Group = 'BDR user pool floor'
$TaskName = 'BDR Agent User Firewall'
$SidAdministrators = 'S-1-5-32-544'
$SidSystem = 'S-1-5-18'

# Must match REQUIRED_REMOTE_RANGES in
# bdr_agent/agents/user_browser_v2/selftest.py (a test pins the two lists).
$BlockedRanges = @(
    '0.0.0.0/8',
    '10.0.0.0/8',
    '100.64.0.0/10',
    '169.254.0.0/16',
    '172.16.0.0/12',
    '192.0.0.0/24',
    '192.0.2.0/24',
    '192.168.0.0/16',
    '198.18.0.0/15',
    '198.51.100.0/24',
    '203.0.113.0/24',
    '224.0.0.0/4',
    '240.0.0.0/4',
    '::/128',
    '::ffff:0:0/96',
    '64:ff9b:1::/48',
    '2001:db8::/32',
    'fc00::/7',
    'fe80::/10',
    'fec0::/10',
    'ff00::/8',
    '64:ff9b::a00:0/104',
    '64:ff9b::ac10:0/108',
    '64:ff9b::c0a8:0/112',
    '64:ff9b::a9fe:0/112',
    '64:ff9b::6440:0/106',
    '64:ff9b::/104',
    '2002:a00::/24',
    '2002:ac10::/28',
    '2002:c0a8::/32',
    '2002:a9fe::/32',
    '2002:6440::/26',
    '2002::/24'
)
$Keywords = @('LocalSubnet')

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Stop-Apply([string]$Message) {
    Write-Host ''
    Write-Host "STOPPED: $Message" -ForegroundColor Red
    exit 1
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-ReparseFree([string]$Path, [string]$StopAt) {
    # True when $StopAt and every path below it down to $Path exist and none
    # is a reparse point (junction, symlink, mount point). Walks top down and
    # reads only attributes, so a link is never followed: the SYSTEM task never
    # acts on a path the service account could have swapped for a link.
    $stop = [IO.Path]::GetFullPath($StopAt).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full.Equals($stop, [StringComparison]::OrdinalIgnoreCase)) { $rest = @() }
    elseif ($full.StartsWith($stop + '\', [StringComparison]::OrdinalIgnoreCase)) {
        $rest = @($full.Substring($stop.Length + 1).Split('\') | Where-Object { $_ })
    } else { return $false }
    $cur = $stop
    $i = 0
    while ($true) {
        try { $item = Get-Item -LiteralPath $cur -Force -ErrorAction Stop } catch { return $false }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        if ($i -ge $rest.Count) { return $true }
        $cur = Join-Path $cur $rest[$i]
        $i++
    }
}

function Assert-SafeFolder([string]$Path) {
    # Interactive run only: refuse a managed folder (or any folder between it
    # and ProgramData) that is a reparse point, so icacls never follows a link
    # planted by the service account into an admin-only folder.
    $root = [IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Apply "$Path must be under $root."
    }
    $cur = $root
    foreach ($part in @($full.Substring($root.Length + 1).Split('\') | Where-Object { $_ })) {
        $cur = Join-Path $cur $part
        if (-not (Test-Path -LiteralPath $cur)) { return }
        if ((Get-Item -LiteralPath $cur -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Stop-Apply "$cur is a junction or link. Remove it by hand and run this script again."
        }
    }
}

function Assert-NoReparseInside([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $entry = Get-ChildItem -LiteralPath $Path -Recurse -Force -Attributes ReparsePoint -ErrorAction Stop | Select-Object -First 1
    if ($entry) { Stop-Apply "$($entry.FullName) is a junction or link. Remove it by hand and run this script again." }
}

function Resolve-VenvHome([string]$Value) {
    # A pyvenv.cfg is writable by the service account, so its home is trusted
    # only when it is a rooted local drive path (never \\server\share) under
    # Program Files or <InstallRoot>\python with no reparse point on the way.
    # Checked before any file system call, so SYSTEM never touches an SMB path.
    if ($Value -notmatch '^[A-Za-z]:\\' -or $Value.Substring(2).Contains(':') -or $Value.Contains('..')) { return $null }
    try { $full = [IO.Path]::GetFullPath($Value).TrimEnd('\') } catch { return $null }
    foreach ($base in @($env:ProgramFiles, (Join-Path $InstallRoot 'python'))) {
        if (-not $base) { continue }
        $b = [IO.Path]::GetFullPath($base).TrimEnd('\')
        if ($full.Equals($b, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase)) {
            if (-not (Test-ReparseFree $full $b)) { return $null }
            $exe = Join-Path $full 'python.exe'
            if (-not (Test-ReparseFree $exe $b)) { return $null }
            return $exe
        }
    }
    return $null
}

function Get-ProgramSet {
    # label -> full path. Rules may name a program that does not exist yet
    # (before the launcher's first sync); Windows applies them once it does.
    $programs = [ordered]@{}
    $runtime = Join-Path $InstallRoot 'runtime\current'
    $programs['node'] = Join-Path $runtime 'driver\node.exe'
    $programs['chrome'] = Join-Path $runtime 'chrome\chrome.exe'
    $pythons = New-Object System.Collections.Generic.List[string]
    $private = Join-Path $InstallRoot 'python\python.exe'
    if (Test-ReparseFree $private $InstallRoot) { $pythons.Add($private) }
    $versions = Join-Path $InstallRoot 'versions'
    if (Test-ReparseFree $versions $InstallRoot) {
        foreach ($dir in @(Get-ChildItem -LiteralPath $versions -Directory -Force)) {
            $cfg = Join-Path $dir.FullName 'venv\pyvenv.cfg'
            if (-not (Test-ReparseFree $cfg $InstallRoot)) { continue }
            foreach ($line in Get-Content -LiteralPath $cfg) {
                if ($line -match '^\s*home\s*=\s*(.+?)\s*$') {
                    $exe = Resolve-VenvHome $Matches[1]
                    if ($exe -and -not $pythons.Contains($exe)) { $pythons.Add($exe) }
                }
            }
        }
    }
    if ($pythons.Count -eq 0) { Stop-Apply "No worker Python found under $InstallRoot. Install the user pool first (bdr-install.ps1 -Pool user)." }
    $i = 0
    foreach ($p in $pythons) { $programs["python$i"] = [IO.Path]::GetFullPath($p); $i++ }
    return $programs
}

function ConvertTo-Cidr([string]$Text) {
    # '203.0.113.7' -> '203.0.113.7/32'; '2001:db8::/48' kept. $null when invalid.
    $t = ([string]$Text).Trim()
    if (-not $t) { return $null }
    $addrText = $t
    $prefix = $null
    if ($t.Contains('/')) {
        $parts = $t.Split('/')
        if ($parts.Count -ne 2) { return $null }
        $addrText = $parts[0]
        $n = 0
        if (-not [int]::TryParse($parts[1], [ref]$n)) { return $null }
        $prefix = $n
    }
    $ip = $null
    if (-not [Net.IPAddress]::TryParse($addrText, [ref]$ip)) { return $null }
    $max = if ($ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) { 128 } else { 32 }
    if ($ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and $addrText -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { return $null }
    if ($null -eq $prefix) { $prefix = $max }
    if ($prefix -lt 8 -or $prefix -gt $max) { return $null }
    return "$($ip.ToString())/$prefix"
}

function Get-WanIPv4 {
    # The office's public IPv4 as the internet sees it. OpenDNS answers
    # myip.opendns.com with the asking address.
    try {
        $answer = Resolve-DnsName -Name 'myip.opendns.com' -Server 'resolver1.opendns.com' -Type A -DnsOnly -QuickTimeout -ErrorAction Stop |
            Where-Object { $_.Type -eq 'A' } | Select-Object -First 1
        if ($answer -and (ConvertTo-Cidr $answer.IPAddress)) { return [string]$answer.IPAddress }
    } catch { }
    return $null
}

function Get-SiteIPv6Prefixes {
    # The /56 of every global IPv6 address learned from a router advertisement:
    # other office VLANs are routed inside the site prefix and are "global",
    # so neither the proxy's rule nor LocalSubnet blocks them.
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($a in @(Get-NetIPAddress -AddressFamily IPv6 -PrefixOrigin RouterAdvertisement -ErrorAction SilentlyContinue)) {
        $ip = $null
        if (-not [Net.IPAddress]::TryParse(([string]$a.IPAddress).Split('%')[0], [ref]$ip)) { continue }
        $bytes = $ip.GetAddressBytes()
        if (($bytes[0] -band 0xE0) -ne 0x20) { continue }   # 2000::/3 global unicast only
        for ($i = 7; $i -lt 16; $i++) { $bytes[$i] = 0 }
        $cidr = "$((New-Object Net.IPAddress (,$bytes)).ToString())/56"
        if (-not $out.Contains($cidr)) { $out.Add($cidr) }
    }
    return ,$out
}

function Get-ExtraRanges($Previous) {
    $explicit = New-Object System.Collections.Generic.List[string]
    $source = $OfficeBlockedRanges
    if ((-not $source -or $source.Count -eq 0) -and $Previous -and ($Previous.PSObject.Properties.Name -contains 'office_ranges')) {
        $source = @($Previous.office_ranges)
    }
    foreach ($r in @($source)) {
        if (-not [string]$r) { continue }
        $c = ConvertTo-Cidr ([string]$r)
        if (-not $c) { Stop-Apply "-OfficeBlockedRanges: '$r' is not an IPv4 or IPv6 address or CIDR range." }
        if (-not $explicit.Contains($c)) { $explicit.Add($c) }
    }
    $wan = $null
    if (-not $NoWanDetect) { $wan = Get-WanIPv4 }
    if (-not $wan -and $Previous -and ($Previous.PSObject.Properties.Name -contains 'wan_ipv4') -and $Previous.wan_ipv4) {
        $wan = [string]$Previous.wan_ipv4
    }
    $all = New-Object System.Collections.Generic.List[string]
    foreach ($c in $explicit) { if (-not $all.Contains($c)) { $all.Add($c) } }
    if ($wan) { $c = ConvertTo-Cidr $wan; if ($c -and -not $all.Contains($c)) { $all.Add($c) } }
    foreach ($c in (Get-SiteIPv6Prefixes)) { if (-not $all.Contains($c)) { $all.Add($c) } }
    return [pscustomobject]@{ explicit = @($explicit); wan = $wan; all = @($all) }
}

function Get-RangesTag {
    $sha = [Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($RuleRanges -join ',')))
    return 'BDR floor ' + (-join ($bytes[0..7] | ForEach-Object { $_.ToString('x2') }))
}

function Get-RuleName([string]$Kind, [string]$Program) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Program.ToLowerInvariant()))
    $hex = -join ($bytes[0..5] | ForEach-Object { $_.ToString('x2') })
    return "BDR-UserPool-$Kind-$hex"
}

function Test-RulesCurrent($Programs) {
    $rules = @(Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue)
    foreach ($kind in @('Ranges', 'Subnet')) {
        foreach ($prog in $Programs.Values) {
            $name = Get-RuleName $kind $prog
            $rule = $rules | Where-Object { $_.Name -eq $name } | Select-Object -First 1
            if (-not $rule) { return $false }
            if ([string]$rule.Enabled -ne 'True' -or [string]$rule.Direction -ne 'Outbound' -or
                [string]$rule.Action -ne 'Block' -or [string]$rule.Profile -ne 'Any') { return $false }
            $app = $rule | Get-NetFirewallApplicationFilter
            if (-not ([string]$app.Program).Equals($prog, [StringComparison]::OrdinalIgnoreCase)) { return $false }
            $addr = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress)
            $want = if ($kind -eq 'Ranges') { $RuleRanges.Count } else { $Keywords.Count }
            if ($addr.Count -lt $want) { return $false }
            if ($kind -eq 'Ranges' -and [string]$rule.Description -ne (Get-RangesTag)) { return $false }
        }
    }
    $expected = 2 * $Programs.Count
    $programRules = @($rules | Where-Object { $_.Name -like 'BDR-UserPool-Ranges-*' -or $_.Name -like 'BDR-UserPool-Subnet-*' })
    return ($programRules.Count -eq $expected)
}

function Set-Rules($Programs) {
    Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    foreach ($entry in $Programs.GetEnumerator()) {
        $prog = $entry.Value
        New-NetFirewallRule -Name (Get-RuleName 'Ranges' $prog) -DisplayName "BDR user pool: block private networks ($($entry.Key))" `
            -Group $Group -Direction Outbound -Action Block -Profile Any -Enabled True `
            -Description (Get-RangesTag) -Program $prog -RemoteAddress $RuleRanges | Out-Null
        New-NetFirewallRule -Name (Get-RuleName 'Subnet' $prog) -DisplayName "BDR user pool: block local subnet ($($entry.Key))" `
            -Group $Group -Direction Outbound -Action Block -Profile Any -Enabled True `
            -Program $prog -RemoteAddress $Keywords | Out-Null
    }
}

function Set-UserRule {
    try {
        $sid = (New-Object Security.Principal.NTAccount("$env:COMPUTERNAME\$ServiceUser")).Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {
        Write-Warning "Service account $ServiceUser not found; the account-wide rule was skipped."
        return $false
    }
    try {
        New-NetFirewallRule -Name 'BDR-UserPool-Account' -DisplayName 'BDR user pool: block private networks (service account)' `
            -Group $Group -Direction Outbound -Action Block -Profile Any -Enabled True `
            -LocalUser "D:(A;;CC;;;$sid)" -RemoteAddress $RuleRanges | Out-Null
        return $true
    } catch {
        Write-Warning "Windows refused the account-wide rule ($($_.Exception.Message)); the program rules still apply."
        return $false
    }
}

function Test-TcpPort([string]$HostName, [int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if ($task.Wait(2000) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-GatewayProbe {
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
    if (-not $route -or -not $route.NextHop -or $route.NextHop -eq '0.0.0.0') { return $null }
    $gw = [string]$route.NextHop
    $ports = @()
    foreach ($p in @(80, 443, 53)) { if (Test-TcpPort $gw $p) { $ports += $p } }
    return [pscustomobject]@{ gateway = $gw; ports = $ports }
}

function Set-FolderAcl([string]$Path, [string]$AgentRights) {
    Assert-SafeFolder $Path
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    Assert-SafeFolder $Path
    Assert-NoReparseInside $Path
    # The service account may have created the folder first (it can write
    # ProgramData and the install root); an owner can rewrite the ACL, so
    # Administrators take ownership before the ACL is set. /L: never follow links.
    & icacls $Path /setowner "*$SidAdministrators" /T /C /L | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Apply "Could not take ownership of $Path." }
    & icacls $Path /inheritance:r /grant:r "*${SidAdministrators}:(OI)(CI)F" "*${SidSystem}:(OI)(CI)F" "$env:COMPUTERNAME\${ServiceUser}:$AgentRights" /L | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Apply "Could not set permissions on $Path." }
}

function Test-QuarantineExcluded {
    $root = [IO.Path]::GetFullPath($QuarantineRoot).TrimEnd('\')
    try { $exclusions = @((Get-MpPreference).ExclusionPath) } catch { return $null }
    foreach ($ex in $exclusions) {
        if (-not $ex) { continue }
        $text = [Environment]::ExpandEnvironmentVariables([string]$ex).TrimEnd('\')
        $cut = $text.IndexOfAny([char[]]'*?')
        if ($cut -ge 0) { $prefix = $text.Substring(0, $cut) } else { $prefix = $text }
        if ($root.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            $prefix.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return [string]$ex }
    }
    return ''
}

# Main ---------------------------------------------------------------------------------------

if (-not (Test-Admin)) { Stop-Apply 'Run this from an elevated PowerShell (Run as Administrator).' }

if ($Remove) {
    Write-Step 'Removing the user pool firewall rules and task'
    Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Removed. The worker self-test now refuses personal browser jobs on this PC.'
    exit 0
}

# Folder ACLs are set once, by an administrator. The SYSTEM re-apply task never
# runs icacls or creates folders: the service account can replace jobs and
# runtime (and could create the quarantine first), and icacls run as SYSTEM on
# a junction would hand it an admin-only folder.
$firewallDir = Join-Path $InstallRoot 'firewall'
if (-not $Reapply) {
    Write-Step 'Folders'
    Set-FolderAcl $firewallDir '(OI)(CI)RX'
    # Quarantine: the service account adds and deletes job folders inside it
    # but cannot delete, rename or re-ACL the root itself.
    Set-FolderAcl $QuarantineRoot '(OI)(CI)(IO)M'
    & icacls $QuarantineRoot /grant "$env:COMPUTERNAME\${ServiceUser}:(RX,WD,AD)" /L | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Apply "Could not set permissions on $QuarantineRoot." }
    foreach ($d in @((Join-Path $InstallRoot 'jobs'), (Join-Path $InstallRoot 'runtime'))) {
        Assert-SafeFolder $d
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
        Assert-SafeFolder $d
        & icacls $d /grant "$env:COMPUTERNAME\${ServiceUser}:(OI)(CI)M" /L | Out-Null
    }
} elseif (-not (Test-ReparseFree $firewallDir $InstallRoot)) {
    Stop-Apply "$firewallDir is missing or is a link. Run this script once by hand as Administrator."
}

$floorFile = Join-Path $firewallDir 'floor.json'
$previous = $null
if (Test-Path -LiteralPath $floorFile) {
    try { $previous = Get-Content -LiteralPath $floorFile -Raw | ConvertFrom-Json } catch { $previous = $null }
}

Write-Step 'Office-owned public addresses'
$extra = Get-ExtraRanges $previous
$ExtraRanges = @($extra.all)
$RuleRanges = @($BlockedRanges) + $ExtraRanges
if ($ExtraRanges.Count -gt 0) { foreach ($r in $ExtraRanges) { Write-Host "    $r" } }
if (-not $extra.wan) {
    Write-Warning 'The office WAN IPv4 is unknown (lookup failed or -NoWanDetect). Pass it with -OfficeBlockedRanges so pages cannot reach the office through hairpin NAT.'
}

Write-Step 'Firewall rules'
$programs = Get-ProgramSet
foreach ($entry in $programs.GetEnumerator()) { Write-Host ("    {0,-8} {1}" -f $entry.Key, $entry.Value) }
$userRule = $false
if (Test-RulesCurrent $programs) {
    Write-Host 'Rules already current.'
    $userRule = [bool](Get-NetFirewallRule -Name 'BDR-UserPool-Account' -ErrorAction SilentlyContinue)
} else {
    Set-Rules $programs
    if (-not $NoUserRule) { $userRule = Set-UserRule }
    Write-Host "Rules written to group '$Group'."
}

Write-Step 'Gateway probe'
$probe = Get-GatewayProbe
if (-not $probe) {
    Write-Warning 'No IPv4 default gateway found. The worker self-test needs one and will refuse personal jobs.'
} elseif ($probe.ports.Count -eq 0) {
    Write-Warning "The gateway $($probe.gateway) answers on none of 80, 443, 53. The self-test cannot prove the block and will refuse personal jobs."
}
if ($probe -and $probe.ports.Count -gt 0) {
    $gateway = $probe.gateway
    $ports = @($probe.ports)
} elseif ($previous) {
    $gateway = [string]$previous.gateway
    $ports = @($previous.gateway_ports)
} else {
    $gateway = $null
    $ports = @()
}
$floor = [ordered]@{
    format = 1
    applied_at = (Get-Date).ToUniversalTime().ToString('o')
    group = $Group
    gateway = $gateway
    gateway_ports = $ports
    programs = $programs
    ranges = $BlockedRanges
    extra_blocked = $ExtraRanges
    office_ranges = @($extra.explicit)
    wan_ipv4 = $extra.wan
    keywords = $Keywords
    account_rule = $userRule
    quarantine_root = $QuarantineRoot
}
$tmp = "$floorFile.tmp"
($floor | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $tmp -Encoding UTF8
Move-Item -LiteralPath $tmp -Destination $floorFile -Force
Write-Host "Gateway $gateway, ports $($ports -join ', ') recorded in $floorFile"

Write-Step 'Microsoft Defender'
$excluded = Test-QuarantineExcluded
if ($null -eq $excluded) {
    Write-Warning 'Defender settings could not be read. The worker refuses downloads until its EICAR check passes.'
} elseif ($excluded) {
    Write-Warning "The Defender exclusion '$excluded' covers $QuarantineRoot. Remove it: the worker refuses every download while it exists."
} else {
    Write-Host "No exclusion covers $QuarantineRoot."
}

if (-not $Reapply -and -not $NoScheduledTask) {
    Write-Step "Scheduled task '$TaskName'"
    $copy = Join-Path $firewallDir 'apply_user_pool_firewall.ps1'
    if (-not $PSCommandPath.Equals($copy, [StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $copy -Force
    }
    $argList = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$copy`" -Reapply " +
               "-InstallRoot `"$InstallRoot`" -QuarantineRoot `"$QuarantineRoot`" -ServiceUser `"$ServiceUser`""
    if ($NoUserRule) { $argList += ' -NoUserRule' }
    if ($NoWanDetect) { $argList += ' -NoWanDetect' }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argList
    $triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650))
    )
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host 'Registered: runs at startup and every 15 minutes as SYSTEM.'
}

Write-Host ''
Write-Host 'Done. Restart the BDRAgentUser service so the worker runs its boot self-test:' -ForegroundColor Green
Write-Host '    Restart-Service BDRAgentUser'
