#!/bin/bash
# Publish the prepared hub and RFIs update; existing releases stay immutable.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/publish_agent_worker_release.py \
  --version 1.0.6 \
  --min-version 1.0.0 \
  --bundle dist/phase6-worker-1.0.6/bdr-agent-1.0.6.zip \
  --output-dir dist/phase6-worker-1.0.6 \
  --resumable \
  --key "$HOME/.bdr/agent_release_ed25519.pem" \
  --expected-project bpidntbyvoooqvaispup \
  --notes 'Allow the Procore hub, project header and RFIs page code from exact hosts as isolated GET assets, add exact-query project reads for modern seeds, and attribute blocked page code per page load in redacted diagnostics. The RFIs tool feature flag request is answered on the PC with an empty flag set, so nothing is sent to LaunchDarkly; every other LaunchDarkly request stays refused. Keep reads disabled until the updated worker is verified; a new read requires a new owner approval.'
