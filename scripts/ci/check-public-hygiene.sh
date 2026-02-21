#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

ALLOWLIST_FILE="scripts/ci/public-hygiene-allowlist.txt"

if [[ ! -f "$ALLOWLIST_FILE" ]]; then
  echo "Missing allowlist file: $ALLOWLIST_FILE" >&2
  exit 1
fi

is_allowed() {
  local rule="$1"
  local file="$2"
  local line="$3"

  grep -Fqx "$rule|$file" "$ALLOWLIST_FILE" && return 0
  grep -Fqx "$rule|$file:$line" "$ALLOWLIST_FILE" && return 0
  return 1
}

check_rule() {
  local rule="$1"
  local regex="$2"
  local message="$3"

  local matches=()
  mapfile -t matches < <(git grep -nI -E -e "$regex" -- . || true)

  local found=0
  for match in "${matches[@]}"; do
    local file="${match%%:*}"
    local rest="${match#*:}"
    local line="${rest%%:*}"

    if is_allowed "$rule" "$file" "$line"; then
      continue
    fi

    if [[ $found -eq 0 ]]; then
      echo ""
      echo "❌ $message"
      found=1
    fi

    echo "  - $match"
  done

  return "$found"
}

failures=0

if ! check_rule "HOME_PATH" '/(home|Users)/[A-Za-z0-9._-]+/' "Hardcoded private home-directory paths detected"; then
  failures=1
fi

if ! check_rule "CHAT_ID" '-100[0-9]{8,}' "Telegram chat IDs detected"; then
  failures=1
fi

if ! check_rule "PHONE_NUMBER" '\+[1-9][0-9]{9,14}' "Phone-like IDs detected (E.164 format)"; then
  failures=1
fi

if ! check_rule "VAULT_NAME" '(Obsidian/[A-Za-z0-9._-]+/(Personal|Private|Family|Home|Journal|Second[-_ ]Brain))' "Personal vault names in default examples detected"; then
  failures=1
fi

if [[ "$failures" -ne 0 ]]; then
  echo ""
  echo "Public hygiene checks failed."
  echo "If a hit is intentional, add an allowlist entry to $ALLOWLIST_FILE:"
  echo "  RULE|path/to/file"
  echo "  RULE|path/to/file:line"
  exit 1
fi

echo "✅ Public hygiene checks passed."
