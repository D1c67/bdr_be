"""Static checks on the Windows install kit (no Windows here): the rendered
installer, the WinSW template, the launcher being standard-library only, and
the repo rules (no em dashes)."""

import ast
import base64
import io
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from render_installer import PLACEHOLDERS, launcher_zip_bytes, render_installer

from .release_helpers import ReleaseKey, manifest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install"
LAUNCHER = ROOT / "launcher"
W5_FILES = [INSTALL / "install.ps1", INSTALL / "uninstall.ps1", INSTALL / "winsw" / "BDRAgent.xml",
            ROOT / "README_WINDOWS.md", ROOT / "bdr_agent" / "updater.py",
            ROOT / "tools" / "build_release_bundle.py", ROOT / "tools" / "render_installer.py",
            *sorted((LAUNCHER / "bdr_launcher").glob("*.py")), LAUNCHER / "launcher.py"]


def _assignment(text: str, var: str) -> str:
    m = re.search(rf"^\${var}\s*=\s*'([^']*)'", text, re.M)
    assert m, var
    return m.group(1)


def test_no_em_dashes_in_w5_files():
    for f in W5_FILES:
        assert "\u2014" not in f.read_text(encoding="utf-8"), f


def test_launcher_is_standard_library_only():
    allowed = set(sys.stdlib_module_names) | {"bdr_launcher", "__future__"}
    for f in [LAUNCHER / "launcher.py", *(LAUNCHER / "bdr_launcher").glob("*.py")]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [] if node.level else [node.module or ""]
            else:
                continue
            for n in names:
                assert n.split(".")[0] in allowed, f"{f.name} imports {n}"


def test_rendered_installer_carries_key_launcher_xml_and_release(tmp_path):
    key = ReleaseKey()
    pub = key.write(tmp_path / "release_key.pub")
    m = manifest("1.1.0", "ab" * 32)
    rel = {"manifest": m, "signature": key.sign(m), "url": "https://storage.test/r.zip?token=t"}
    text = render_installer("https://api.bdr.test/", pub, rel)
    for token in PLACEHOLDERS.values():
        assert token not in text
    assert _assignment(text, "EmbeddedApiBase") == "https://api.bdr.test"
    assert base64.b64decode(_assignment(text, "EmbeddedPubKeyB64")) == key.raw
    zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(_assignment(text, "EmbeddedLauncherB64"))))
    names = set(zf.namelist())
    assert "launcher.py" in names and "bdr_launcher/release.py" in names and "bdr_launcher/ed25519.py" in names
    assert not any("__pycache__" in n for n in names)
    xml = base64.b64decode(_assignment(text, "EmbeddedWinswXmlB64"))
    assert xml == (INSTALL / "winsw" / "BDRAgent.xml").read_bytes()
    carried = json.loads(base64.b64decode(_assignment(text, "EmbeddedReleaseB64")))
    assert carried["manifest"] == m and carried["signature"] == rel["signature"]
    # Deterministic launcher payload.
    assert launcher_zip_bytes() == launcher_zip_bytes()


def test_render_refuses_bad_inputs(tmp_path):
    key = ReleaseKey()
    pub = key.write(tmp_path / "release_key.pub")
    m = manifest("1.1.0")
    with pytest.raises(ValueError):
        render_installer("http://api.bdr.test", pub)
    with pytest.raises(Exception):   # forged release signature
        render_installer("https://api.bdr.test", pub, {"manifest": m, "signature": ReleaseKey().sign(m),
                                                       "url": "https://s.test/x"})
    text = render_installer("https://api.bdr.test", pub)          # no release: offline flags needed
    assert _assignment(text, "EmbeddedReleaseB64") == ""


def test_embedded_launcher_runs_without_site_packages(tmp_path):
    """The launcher must work on the bare runtime: run it with -I -S (no
    site-packages at all) from the zip the installer carries."""
    key = ReleaseKey()
    dest = tmp_path / "launcher"
    zipfile.ZipFile(io.BytesIO(launcher_zip_bytes())).extractall(dest)
    pub = key.write(tmp_path / "k.pub")
    out = subprocess.run([sys.executable, "-I", "-S", str(dest / "launcher.py"), "--home", str(tmp_path / "h"),
                          "fingerprint", "--key", str(pub)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    from bdr_launcher.release import fingerprint

    assert out.stdout.strip() == fingerprint(key.raw)
    st = subprocess.run([sys.executable, "-I", "-S", str(dest / "launcher.py"), "--home", str(tmp_path / "h"),
                         "status"], capture_output=True, text=True, timeout=60)
    assert st.returncode == 0 and json.loads(st.stdout)["current"] is None


def test_winsw_template_shape_and_password_scrub():
    raw = (INSTALL / "winsw" / "BDRAgent.xml").read_text(encoding="utf-8")
    filled = (raw.replace("{{PYTHON}}", r"C:\ProgramData\BDRAgent\python\python.exe")
              .replace("{{HOME}}", r"C:\ProgramData\BDRAgent").replace("{{SERVICE_USER}}", "bdr-agent")
              .replace("{{SERVICE_PASSWORD}}", "Secret-Pass.123"))
    root = ET.fromstring(filled)
    assert root.findtext("id") == "BDRAgent"
    assert root.findtext("startmode") == "Automatic"
    assert root.findtext("delayedAutoStart") == "true"
    assert [e.get("delay") for e in root.findall("onfailure")] == ["10 sec", "30 sec", "60 sec"]
    assert all(e.get("action") == "restart" for e in root.findall("onfailure"))
    assert "launcher.py" in root.findtext("arguments") and root.findtext("arguments").startswith("-I ")
    acct = root.find("serviceaccount")
    assert acct.findtext("user") == "bdr-agent" and acct.findtext("allowservicelogon") == "true"
    assert "LocalSystem" not in ET.tostring(root, encoding="unicode")
    # The same scrub install.ps1 applies after `install`.
    ps1 = (INSTALL / "install.ps1").read_text(encoding="utf-8")
    pattern = re.search(r"\[Regex\]::Replace\(\$xml, '([^']+)'", ps1).group(1)
    scrubbed = re.sub(pattern, "<!-- removed -->", filled)
    assert "Secret-Pass.123" not in scrubbed
    ET.fromstring(scrubbed)


def test_install_script_pins_and_flow_markers():
    ps1 = (INSTALL / "install.ps1").read_text(encoding="utf-8")
    for var in ("PythonSha256", "WinswSha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", _assignment(ps1, var))
    assert _assignment(ps1, "PythonUrl").startswith("https://www.python.org/")
    assert _assignment(ps1, "WinswUrl").startswith("https://github.com/winsw/winsw/releases/download/v2.12.0/")
    # Order: fingerprint confirmation comes before any change on the PC.
    first_change = min(ps1.index(s) for s in ("New-LocalUser", "New-Item -ItemType Directory -Path (Join-Path $InstallRoot",
                                              "Start-Process -FilePath $pyInstaller"))
    assert ps1.index("Type yes if the fingerprint matches") < first_change
    assert "install-bundle" in ps1 and "-Credential $Credential -LoadUserProfile" in ps1
    assert "Get-AuthenticodeSignature" in ps1
    for placeholder in PLACEHOLDERS.values():
        assert ps1.count(placeholder) == 1


def test_installer_places_its_own_uninstaller_on_the_pc():
    """The rendered installer is one file; it must carry uninstall.ps1 (inside
    the launcher zip) and put it in <home>\\service, where the docs point."""
    zf = zipfile.ZipFile(io.BytesIO(launcher_zip_bytes()))
    assert zf.read("uninstall.ps1") == (INSTALL / "uninstall.ps1").read_bytes()
    text = (INSTALL / "install.ps1").read_text(encoding="utf-8")
    assert "Join-Path $InstallRoot 'service\\uninstall.ps1'" in text
    assert "Move-Item -LiteralPath $UninstallSrc -Destination $UninstallDest" in text
    assert "run uninstall.ps1." not in text and "Run uninstall.ps1 first" not in text
    readme = (INSTALL.parent / "README_WINDOWS.md").read_text(encoding="utf-8")
    assert "C:\\path\\to\\uninstall.ps1" not in readme
    assert "C:\\ProgramData\\BDRAgent\\service\\uninstall.ps1" in readme
