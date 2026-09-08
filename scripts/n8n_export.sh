#!/usr/bin/env bash
# Export n8n workflows via REST API through SSH.
#
# Usage: ./scripts/n8n_export.sh
#
# Requires:
#   - .env.deploy with NAS_HOST, NAS_USER, NAS_PORT, NAS_SSH_KEY, NAS_SUDO_PASSWORD
#   - n8n/.env with N8N_API_KEY
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${PROJECT_ROOT}/.env.deploy"

# Load n8n API key
N8N_ENV="${PROJECT_ROOT}/n8n/.env"
N8N_API_KEY=$(grep -E '^N8N_API_KEY=' "${N8N_ENV}" | cut -d'=' -f2- | tr -d '"' | tr -d "'")
if [[ -z "${N8N_API_KEY}" ]]; then
  echo "✘ N8N_API_KEY not set in n8n/.env" >&2
  exit 1
fi

SSH_KEY="${NAS_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
SSH_DEST="${NAS_USER}@${NAS_HOST}"
SSH_PORT="${NAS_PORT:-2222}"
OUT_DIR="${PROJECT_ROOT}/n8n/workflows"

# ── Colors ──
C_RESET='\033[0m'; C_GREEN='\033[0;32m'; C_CYAN='\033[0;36m'; C_RED='\033[0;31m'
log()  { printf "${C_CYAN}▶ %s${C_RESET}\n" "$*"; }
ok()   { printf "${C_GREEN}✔ %s${C_RESET}\n" "$*"; }
err()  { printf "${C_RED}✘ %s${C_RESET}\n" "$*" >&2; }

# ── SSH wrapper: run curl on NAS (n8n is localhost:5678 inside NAS) ──
remote_curl() {
  ssh -n -o StrictHostKeyChecking=no -i "${SSH_KEY}" -p "${SSH_PORT}" "${SSH_DEST}" \
    "curl -sf $1 http://localhost:5678/api/v1$2 -H 'X-N8N-API-KEY: ${N8N_API_KEY}'"
}

mkdir -p "${OUT_DIR}"

# ── Step 1: Fetch all workflows ──
log "Fetching workflows from n8n ..."
WORKFLOWS=$(remote_curl "" "/workflows")

if [[ -z "${WORKFLOWS}" ]]; then
  err "No response from n8n API"
  exit 1
fi

# Check for actual API errors (not workflow content that may contain "error")
API_STATUS=$(echo "${WORKFLOWS}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if 'data' in d:
    print('ok')
elif 'message' in d:
    print(d['message'])
else:
    print('unknown')
" 2>/dev/null || echo "parse_error")

if [[ "${API_STATUS}" != "ok" ]]; then
  err "API error: ${API_STATUS}"
  exit 1
fi

COUNT=$(echo "${WORKFLOWS}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('data',d) if isinstance(d,dict) else d))")
log "Found ${COUNT} workflow(s)"

if [[ "${COUNT}" -eq 0 ]]; then
  err "No workflows found."
  exit 1
fi

# ── Step 2: Export each workflow ──
# Clean old files
rm -f "${OUT_DIR}"/*.json

WORKFLOW_IDS=$(echo "${WORKFLOWS}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
wfs = d.get('data', d) if isinstance(d, dict) else d
for wf in wfs:
    print(f\"{wf['id']}|{wf.get('name','unnamed')}\")
")

EXPORTED=0
while IFS='|' read -r wf_id wf_name; do
  log "  Exporting: ${wf_name} (${wf_id})"

  # Fetch full workflow
  WF_JSON=$(remote_curl "" "/workflows/${wf_id}")

  # Sanitize filename
  SAFE_NAME=$(echo "${wf_name}" | python3 -c "import sys,re; print(re.sub(r'[^\w\s-]','',sys.stdin.read().strip()).replace(' ','_'))")
  FILENAME="${SAFE_NAME}__${wf_id}.json"

  echo "${WF_JSON}" | python3 -c "import json,sys; json.dump(json.load(sys.stdin), open('${OUT_DIR}/${FILENAME}','w'), indent=2, ensure_ascii=False)"

  ok "    ${FILENAME}"
  ((EXPORTED++)) || true
done <<< "${WORKFLOW_IDS}"

echo ""
ok "Exported ${EXPORTED} workflow(s) → ${OUT_DIR}/"
echo ""
echo "Files:"
ls -la "${OUT_DIR}"/*.json 2>/dev/null | awk '{print "  " $NF " (" $5 " bytes)"}'
echo ""
echo "Next steps:"
echo "  git add n8n/workflows/"
echo "  git commit -m 'backup(n8n): export workflows'"
