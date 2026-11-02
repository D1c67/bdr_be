#!/bin/bash
# Publish the prepared transport update; existing releases stay immutable.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/publish_agent_worker_release.py \
  --version 1.0.4 \
  --min-version 1.0.0 \
  --bundle dist/phase6-worker-1.0.4/bdr-agent-1.0.4.zip \
  --output-dir dist/phase6-worker-1.0.4 \
  --resumable \
  --key "$HOME/.bdr/agent_release_ed25519.pem" \
  --expected-project bpidntbyvoooqvaispup \
  --notes 'Bounded Playwright driver response-header limit and precise redacted transport diagnostics. Keep reads disabled until the updated worker is verified; a new read requires a new owner approval.'
