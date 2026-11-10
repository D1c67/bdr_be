"""Source-level guards for the Windows handoff (PowerShell execution is a PC check)."""
from pathlib import Path
import re


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "phase6_reconnect_personal_worker.ps1"


def test_reconnect_is_bound_to_the_enrolled_personal_service():
    text = SCRIPT.read_text()
    assert ("param([Parameter(Mandatory = $true)][string]$ApiBase, "
            "[Parameter(Mandatory = $true)][string]$ExpectedWorkerId)") in text
    assert "$WorkerHome = 'C:\\ProgramData\\BDRAgentUser'" in text
    assert "$ServiceName = 'BDRAgentUser'" in text
    # Browser v2 rollout re-enrolls the service (a new worker row), so the id
    # is a parameter from the Mac helper, never pinned in the script.
    assert not re.search(r"\$ExpectedWorkerId\s*=", text)
    pattern = re.search(r"\$ExpectedWorkerId -cnotmatch '([^']+)'", text).group(1)
    assert re.fullmatch(pattern, "e0846bf6-ea57-4927-9d12-de514532a6e3")
    for value in ("E0846BF6-EA57-4927-9D12-DE514532A6E3", "e0846bf6", "x'; Remove-Item", ""):
        assert not re.fullmatch(pattern, value)
    assert text.index("$ExpectedWorkerId -cnotmatch") < text.index("Stop-Service -Name")
    assert "$config.worker_id -cne $ExpectedWorkerId" in text
    assert "$install.pool -cne 'user'" in text
    assert "$serviceInfo.PathName.Trim" in text
    assert "-inotcontains $serviceInfo.StartName" in text
    commands = re.findall(r"(?m)^\s*(?:Stop|Start|Restart)-Service (.+)$", text)
    assert commands and all(command == "-Name $ServiceName -ErrorAction Stop" for command in commands)
    assert not re.search(r"(?m)^\s*(?:Set|New|Remove)-LocalUser\b", text)
    assert "token.bin" in text  # Only a comment: no token operation or path construction.
    assert all(line.lstrip().startswith("#") for line in text.splitlines() if "token.bin" in line)


def test_new_origin_and_health_are_validated_before_stopping():
    text = SCRIPT.read_text()
    pattern = re.search(r"\$ApiBase -cnotmatch '([^']+)'", text).group(1)
    assert re.fullmatch(pattern, "https://four-word-tunnel-name.trycloudflare.com")
    for value in ("http://a.trycloudflare.com", "https://example.com", "https://a.trycloudflare.com/evil",
                  "https://a.trycloudflare.com:443", "https://a.trycloudflare.com?x=1",
                  "https://a.trycloudflare.com.evil.com", "https://user@a.trycloudflare.com"):
        assert not re.fullmatch(pattern, value)
    stop = text.index("Stop-Service -Name")
    assert text.index("-MaximumRedirection 0 -TimeoutSec 20") < stop
    assert text.index("$health.StatusCode -ne 200") < stop
    assert text.index("$install.pool -cne 'user'") < stop
    assert text.index("$null = Read-WorkerConfig") < stop


def test_backup_and_rollback_preserve_existing_bytes_and_acl():
    text = SCRIPT.read_text()
    backup = text.index("[IO.File]::WriteAllBytes($backupPath, $originalBytes)")
    assert text.index("$backupAcl.SetAccessRuleProtection($true, $false)") < backup
    assert text.index("Set-Acl -LiteralPath $backupDir -AclObject $backupAcl") < backup
    assert "$backupAcl.SetOwner($admins)" in text
    assert "@('S-1-5-32-544', 'S-1-5-18')" in text
    assert "$config.api_base_url = $ApiBase" in text
    assert "New-Object Text.UTF8Encoding($false)" in text
    assert text.count("[IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None") == 2
    assert "$stream.SetLength($originalBytes.Length)" in text
    assert "$stream.Write($originalBytes, 0, $originalBytes.Length)" in text
    assert text.count("$stream.Flush($true)") == 2
    assert "Set-Acl -LiteralPath $ConfigPath" not in text
    assert "Remove-Item" not in text and "Move-Item" not in text
    assert text.index("$stopped = $true") < text.index("Stop-Service -Name")
    assert backup < text.index("$writeStarted = $true") < text.index("$stream.Write($updatedBytes")
