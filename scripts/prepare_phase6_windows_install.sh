#!/bin/bash
# Run on the Mac; holds a new development tunnel open for Windows setup.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/prepare_phase6_windows_install.py
