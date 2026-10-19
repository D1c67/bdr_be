"""BDR agent launcher: the stable service entry (spec 8, D16).

WinSW runs `<home>\\python\\python.exe -I <home>\\launcher\\launcher.py run`.
Standard library only; it never changes with worker releases (the installer
replaces it). See bdr_launcher/cli.py for the commands.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bdr_launcher.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
