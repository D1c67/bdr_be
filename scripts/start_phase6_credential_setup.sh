#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/start_phase6_credential_setup.py
