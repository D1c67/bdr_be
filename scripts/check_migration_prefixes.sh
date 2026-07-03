#!/usr/bin/env bash
# Fail if any two migrations share a numeric 000N_ prefix. Migrations are
# applied manually and identified by prefix, so duplicates are a hazard
# (e.g. the 0052_project_number_unique / 0052_quote_tax collision).
# Run from the bdr_be repo root: ./scripts/check_migration_prefixes.sh
set -euo pipefail

cd "$(dirname "$0")/.."

dupes=$(ls supabase/migrations | cut -d_ -f1 | sort | uniq -d)

if [ -n "$dupes" ]; then
  echo "ERROR: duplicate migration prefixes found:" >&2
  echo "$dupes" >&2
  exit 1
fi

echo "OK: no duplicate migration prefixes."
