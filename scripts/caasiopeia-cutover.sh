#!/usr/bin/env bash
# Operator helper: switch the installed OSM mcp-server to the Caasiopeia
# retrieval backend, or back to local. Run it in your own terminal, never
# through an agent shell: `key` prints a secret exactly once.
#
# The API key lives in the macOS Keychain (service osm-caasiopeia-api-key).
# It is never written to a file and never passed as a command argument.
set -euo pipefail

KEYCHAIN_SERVICE="osm-caasiopeia-api-key"
INSTALL_DIR="${OSM_INSTALL_DIR:-$HOME/.local/share/obsidian-semantic-mcp}"
BASE_URL="${CAASIOPEIA_BASE_URL:-http://host.docker.internal:3000}"
VAULT_KEY="${OSM_VAULT_KEY:-vault}"
SOURCE_SUBFOLDER="${CAASIOPEIA_SOURCE_SUBFOLDER:-notes}"

usage() {
  printf '%s\n' \
    "usage: caasiopeia-cutover.sh <command>" \
    "" \
    "  sources   list sources for the tenant (needs CAAS_OWNER_DATABASE_URL," \
    "            CAAS_API_KEY_PEPPER, CAASIOPEIA_TENANT in your environment)" \
    "  key       create an API key and print it once; store it in the Keychain" \
    "            with the command it prints" \
    "  apply     recreate mcp-server on the caasiopeia backend" \
    "            (needs CAASIOPEIA_SOURCE_ID, and the Keychain entry from 'key')" \
    "  rollback  recreate mcp-server on the local backend" \
    "  status    show which backend the running container was created with" \
    "" \
    "optional: OSM_INSTALL_DIR, CAASIOPEIA_BASE_URL (default $BASE_URL)," \
    "  OSM_VAULT_KEY (default vault), CAASIOPEIA_SOURCE_SUBFOLDER (default notes)"
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

require_env() {
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || die "$name is not set"
  done
}

require_admin() {
  command -v caasiopeia-admin >/dev/null 2>&1 || die "caasiopeia-admin not on PATH"
  require_env CAAS_OWNER_DATABASE_URL CAAS_API_KEY_PEPPER CAASIOPEIA_TENANT
}

require_install() {
  [ -f "$INSTALL_DIR/docker-compose.yml" ] || die "no docker-compose.yml in $INSTALL_DIR"
}

cmd_sources() {
  require_admin
  caasiopeia-admin source list --tenant "$CAASIOPEIA_TENANT"
}

cmd_key() {
  require_admin
  local out key_line
  out="$(caasiopeia-admin key create --tenant "$CAASIOPEIA_TENANT")"
  key_line="$(printf '%s\n' "$out" | grep '^api_key=' || true)"
  [ -n "$key_line" ] || die "key create returned no api_key line"
  printf '%s\n' "$out" | grep -v '^api_key='
  printf 'api key (shown once): %s\n' "${key_line#api_key=}"
  printf '\nstore it now (paste at the hidden prompt):\n'
  # shellcheck disable=SC2016  # $USER is shown literally for the operator to run
  printf '  security add-generic-password -U -s %s -a "$USER" -w\n' "$KEYCHAIN_SERVICE"
}

recreate_mcp_server() {
  require_install
  (cd "$INSTALL_DIR" && docker compose up -d --no-deps mcp-server)
}

cmd_apply() {
  require_env CAASIOPEIA_SOURCE_ID
  local api_key
  api_key="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$USER" -w 2>/dev/null)" \
    || die "no Keychain entry $KEYCHAIN_SERVICE; run 'key' first"
  [ -n "$api_key" ] || die "Keychain entry $KEYCHAIN_SERVICE is empty"
  export OSM_RETRIEVAL_BACKEND=caasiopeia
  export CAASIOPEIA_BASE_URL="$BASE_URL"
  export CAASIOPEIA_API_KEY="$api_key"
  export CAASIOPEIA_SOURCE_MAP="$VAULT_KEY=$CAASIOPEIA_SOURCE_ID"
  export CAASIOPEIA_SOURCE_ROOTS="$VAULT_KEY=$SOURCE_SUBFOLDER"
  recreate_mcp_server
  printf 'applied: backend=caasiopeia base_url=%s vault=%s subfolder=%s\n' \
    "$BASE_URL" "$VAULT_KEY" "$SOURCE_SUBFOLDER"
  printf 'a later "osm rebuild" recreates the container without these variables; rerun apply after it.\n'
}

cmd_rollback() {
  unset OSM_RETRIEVAL_BACKEND CAASIOPEIA_BASE_URL CAASIOPEIA_API_KEY \
    CAASIOPEIA_SOURCE_MAP CAASIOPEIA_SOURCE_ROOTS
  recreate_mcp_server
  printf 'rolled back: backend=local\n'
}

cmd_status() {
  require_install
  local cid
  cid="$(cd "$INSTALL_DIR" && docker compose ps -q mcp-server)"
  [ -n "$cid" ] || die "mcp-server is not running"
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" \
    | grep -E '^(OSM_RETRIEVAL_BACKEND|CAASIOPEIA_BASE_URL|CAASIOPEIA_SOURCE_ROOTS)=.+' \
    || printf 'no retrieval variables set: backend=local (default)\n'
}

case "${1:-}" in
  sources) cmd_sources ;;
  key) cmd_key ;;
  apply) cmd_apply ;;
  rollback) cmd_rollback ;;
  status) cmd_status ;;
  *) usage; exit 2 ;;
esac
