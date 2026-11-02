#!/bin/bash
# Sign the already prepared regional Procore update locally; do not rebuild 1.0.1.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/publish_agent_worker_release.py \
  --version 1.0.2 \
  --min-version 1.0.0 \
  --bundle dist/phase6-worker-1.0.2/bdr-agent-1.0.2.zip \
  --output-dir dist/phase6-worker-1.0.2 \
  --resumable \
  --key "$HOME/.bdr/agent_release_ed25519.pem" \
  --expected-project bpidntbyvoooqvaispup \
  --installer-from-settings \
  --notes 'Explicit US02 personal Procore host and project-hub overview support with job-bound regional sessions. Activate only after owner credential refresh and worker verification.'
