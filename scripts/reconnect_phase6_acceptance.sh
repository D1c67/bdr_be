#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/reconnect_phase6_acceptance.py
