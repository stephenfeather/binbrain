#!/usr/bin/env bash
# suggest_photo.sh — Re-run /suggest and /detect against an existing photo.
#
# Usage:
#   suggest_photo.sh <photo_id>
#
# Environment:
#   BINBRAIN_API_URL  Base URL (default: http://localhost:8000)
#   BINBRAIN_API_KEY  Required. X-API-Key value.
#   CURL_TIMEOUT      Max seconds for the suggest request (default: 240; qwen cold can exceed 150s).
#   SKIP_DETECT       Set to 1 to skip the /detect call.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <photo_id>" >&2
  exit 2
fi

photo_id="$1"
if ! [[ "$photo_id" =~ ^[0-9]+$ ]]; then
  echo "Error: photo_id must be a positive integer, got: $photo_id" >&2
  exit 2
fi

api_url="${BINBRAIN_API_URL:-http://localhost:8000}"
api_key="${BINBRAIN_API_KEY:-}"
timeout="${CURL_TIMEOUT:-240}"

if [[ -z "$api_key" ]]; then
  echo "Error: BINBRAIN_API_KEY is not set." >&2
  echo "       Export it with: export BINBRAIN_API_KEY='your-key-here'" >&2
  exit 2
fi

_pretty() {
  local file="$1"
  if command -v jq >/dev/null 2>&1; then
    jq . "$file" 2>/dev/null || cat "$file"
  else
    cat "$file"
  fi
}

# ── /suggest ────────────────────────────────────────────────────────────────
suggest_endpoint="${api_url%/}/photos/${photo_id}/suggest"
echo "=== SUGGEST ===" >&2
echo "GET $suggest_endpoint (timeout ${timeout}s)" >&2

suggest_resp="/tmp/suggest_photo_suggest.$$"
trap 'rm -f "$suggest_resp" "$detect_resp" 2>/dev/null || true' EXIT

start_ns=$(date +%s)
suggest_code=$(
  curl -sS -X GET "$suggest_endpoint" \
    -H "X-API-Key: $api_key" \
    -H "Accept: application/json" \
    --max-time "$timeout" \
    -o "$suggest_resp" \
    -w "%{http_code}" \
    || { echo "curl failed (exit $?) — see $suggest_resp" >&2; exit 3; }
)
elapsed=$(( $(date +%s) - start_ns ))
echo "HTTP $suggest_code · ${elapsed}s elapsed" >&2
_pretty "$suggest_resp"

# ── /detect ─────────────────────────────────────────────────────────────────
if [[ "${SKIP_DETECT:-0}" != "1" ]]; then
  detect_endpoint="${api_url%/}/photos/${photo_id}/detect"
  detect_resp="/tmp/suggest_photo_detect.$$"
  echo "" >&2
  echo "=== DETECT (YOLO bboxes) ===" >&2
  echo "POST $detect_endpoint (timeout 30s)" >&2

  start_ns=$(date +%s)
  detect_code=$(
    curl -sS -X POST "$detect_endpoint" \
      -H "X-API-Key: $api_key" \
      -H "Accept: application/json" \
      --max-time 30 \
      -o "$detect_resp" \
      -w "%{http_code}" \
      2>/dev/null || echo "000"
  )
  elapsed=$(( $(date +%s) - start_ns ))
  echo "HTTP $detect_code · ${elapsed}s elapsed" >&2
  _pretty "$detect_resp"
fi

# Non-2xx suggest exits with 1
if [[ "$suggest_code" != 2* ]]; then
  exit 1
fi
