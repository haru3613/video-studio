#!/usr/bin/env bash
set -euo pipefail

TOKEN_FILE="${REPLICATE_API_TOKEN_FILE:-}"

if [[ -z "${REPLICATE_API_TOKEN:-}" ]]; then
  if [[ ! -f "$TOKEN_FILE" ]]; then
    echo "Set REPLICATE_API_TOKEN or an explicit REPLICATE_API_TOKEN_FILE." >&2
    exit 1
  fi

  REPLICATE_API_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
  export REPLICATE_API_TOKEN
fi

if [[ -z "$REPLICATE_API_TOKEN" ]]; then
  echo "REPLICATE_API_TOKEN is empty" >&2
  exit 1
fi

exec npx --no-install replicate-mcp "$@"
