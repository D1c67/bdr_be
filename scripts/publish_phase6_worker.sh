#!/bin/bash
# Run on the owner's Mac. The signing passphrase stays in the terminal.
# Publishes to development only; does not select the release or enable jobs.
set -euo pipefail
cd "$(dirname "$0")/.."

exec uv run --with pip --with packaging python scripts/publish_agent_worker_release.py \
  --version 1.0.1 \
  --min-version 1.0.0 \
  --key "$HOME/.bdr/agent_release_ed25519.pem" \
  --expected-project bpidntbyvoooqvaispup \
  --output-dir dist/phase6-worker-1.0.1 \
  --installer-from-settings \
  --notes 'Personal browser worker pool, private task protocol and guarded Procore reads. Activation and Windows acceptance are separate.' \
  "$@"
