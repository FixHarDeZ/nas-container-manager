#!/usr/bin/env bash
# Import n8n workflows via REST API through SSH.
#
# Usage: ./scripts/n8n_import.sh [workflow_file.json]
#
# Without arguments: imports ALL .json files from n8n/workflows/
# With argument: imports only the specified file
#
# If a workflow with the same name already exists, it will be UPDATED (not duplicated).
#
# Requires:
#   - .env.deploy with NAS_HOST, NAS_USER, NAS_PORT, NAS_SSH_KEY
#   - n8n/.env with N8N_API_KEY
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${PROJECT_ROOT}/.env.deploy"

N8N_ENV="${PROJECT_ROOT}/n8n/.env"
N8N_API_KEY=$(grep -E '^N8N_API_KEY=' "${N8N_ENV}" | cut -d'=' -f2- | tr -d '"' | tr -d "'")
if [[ -z "${N8N_API_KEY}" ]]; then
  echo "✘ N8N_API_KEY not set in n8n/.env" >&2
  exit 1
fi

SSH_KEY="${NAS_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
SSH_DEST="${NAS_USER}@${NAS_HOST}"
SSH_PORT="${NAS_PORT:-2222}"
WORKFLOW_DIR="${PROJECT_ROOT}/n8n/workflows"

# ── Colors ──
C_RESET='\033[0m'; C_GREEN='\033[0;32m'; C_CYAN='\033[0;36m'; C_RED='\033[0;31m'; C_YELLOW='\033[0;33m'
log()  { printf "${C_CYAN}▶ %s${C_RESET}\n" "$*"; }
ok()   { printf "${C_GREEN}✔ %s${C_RESET}\n" "$*"; }
err()  { printf "${C_RED}✘ %s${C_RESET}\n" "$*" >&2; }
warn() { printf "${C_YELLOW}⚠ %s${C_RESET}\n" "$*"; }

# ── SSH wrapper: send JSON payload via base64 to avoid shell escaping issues ──
remote_api() {
  local method="$1" path="$2" payload_file="${3:-}"
  if [[ -n "${payload_file}" ]]; then
    local b64
    b64=$(base64 -w0 < "${payload_file}")
    ssh -n -o StrictHostKeyChecking=no -i "${SSH_KEY}" -p "${SSH_PORT}" "${SSH_DEST}" \
      "echo '${b64}' | base64 -d > /tmp/_n8n_payload.json && \
       curl -sf -X ${method} 'http://localhost:5678/api/v1${path}' \
         -H 'X-N8N-API-KEY: ${N8N_API_KEY}' \
         -H 'Content-Type: application/json' \
         -d @/tmp/_n8n_payload.json; rm -f /tmp/_n8n_payload.json"
  else
    ssh -n -o StrictHostKeyChecking=no -i "${SSH_KEY}" -p "${SSH_PORT}" "${SSH_DEST}" \
      "curl -sf -X ${method} 'http://localhost:5678/api/v1${path}' \
         -H 'X-N8N-API-KEY: ${N8N_API_KEY}'"
  fi
}

# ── Fetch existing workflows ──
log "Fetching existing workflows for dedup ..."
EXISTING=$(remote_api "GET" "/workflows")
EXISTING_IDS=$(echo "${EXISTING}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
wfs = d.get('data', d) if isinstance(d, dict) else d
for wf in wfs:
    print(f\"{wf['id']}|{wf.get('name','')}\")
" 2>/dev/null || true)

# ── Determine files to import ──
if [[ $# -gt 0 ]]; then
  IMPORT_FILES=("$@")
else
  IMPORT_FILES=()
  for f in "${WORKFLOW_DIR}"/*.json; do
    [[ -f "$f" ]] && IMPORT_FILES+=("$f")
  done
fi

if [[ ${#IMPORT_FILES[@]} -eq 0 ]]; then
  err "No workflow JSON files found to import."
  exit 1
fi

log "Importing ${#IMPORT_FILES[@]} workflow(s) ..."

IMPORTED=0
UPDATED=0
FAILED=0

for workflow_file in "${IMPORT_FILES[@]}"; do
  if [[ ! -f "${workflow_file}" ]]; then
    err "  File not found: ${workflow_file}"
    ((FAILED++)) || true
    continue
  fi

  filename=$(basename "${workflow_file}")
  WF_NAME=$(python3 -c "import json; print(json.load(open('${workflow_file}')).get('name',''))")

  # Check if workflow with same name exists
  EXISTING_ID=$(echo "${EXISTING_IDS}" | grep -F "|${WF_NAME}" | head -1 | cut -d'|' -f1 || true)

  if [[ -n "${EXISTING_ID}" ]]; then
    # Update existing — only send fields accepted by PUT /api/v1/workflows/{id}
    PAYLOAD_FILE=$(mktemp /tmp/n8n_import_XXXXXX.json)
    python3 -c "
import json, sys
d = json.load(open('${workflow_file}'))
minimal = {
    'name': d['name'],
    'nodes': d['nodes'],
    'connections': d['connections'],
    'settings': {'executionOrder': d.get('settings',{}).get('executionOrder','v1')},
}
json.dump(minimal, open('${PAYLOAD_FILE}','w'), ensure_ascii=False)
"
    log "  Updating: ${filename} (id: ${EXISTING_ID})"
    RESULT=$(remote_api "PUT" "/workflows/${EXISTING_ID}" "${PAYLOAD_FILE}" 2>&1) || true
    rm -f "${PAYLOAD_FILE}"

    if echo "${RESULT}" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'id' in d or 'data' in d" 2>/dev/null; then
      ok "    Updated: ${filename}"
      ((UPDATED++)) || true
    else
      err "    Failed: ${filename} — ${RESULT}"
      ((FAILED++)) || true
    fi
  else
    # Create new — only send fields accepted by POST /api/v1/workflows
    PAYLOAD_FILE=$(mktemp /tmp/n8n_import_XXXXXX.json)
    python3 -c "
import json, sys
d = json.load(open('${workflow_file}'))
minimal = {
    'name': d['name'],
    'nodes': d['nodes'],
    'connections': d['connections'],
    'settings': {'executionOrder': d.get('settings', {}).get('executionOrder', 'v1')},
}
if 'pinData' in d:
    minimal['pinData'] = d['pinData']
json.dump(minimal, open('${PAYLOAD_FILE}','w'), ensure_ascii=False)
"
    log "  Creating: ${filename}"
    RESULT=$(remote_api "POST" "/workflows" "${PAYLOAD_FILE}" 2>&1) || true
    rm -f "${PAYLOAD_FILE}"

    if echo "${RESULT}" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'id' in d or 'data' in d" 2>/dev/null; then
      ok "    Created: ${filename}"
      ((IMPORTED++)) || true
    else
      err "    Failed: ${filename} — ${RESULT}"
      ((FAILED++)) || true
    fi
  fi
done

echo ""
if [[ ${FAILED} -eq 0 ]]; then
  ok "Import complete: ${IMPORTED} created, ${UPDATED} updated"
else
  warn "Created: ${IMPORTED}, Updated: ${UPDATED}, Failed: ${FAILED}"
fi
