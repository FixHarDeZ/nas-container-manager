#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env.deploy"
START_TS=$(date +%s)

# ── Colors ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'
  C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'
  C_CYAN='\033[0;36m'; C_RED='\033[0;31m'; C_DIM='\033[2m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_RED=''; C_DIM=''
fi

log()  { printf "${C_BOLD}${C_CYAN}▶ %s${C_RESET}\n" "$*"; }
ok()   { printf "${C_GREEN}✔ %s${C_RESET}\n" "$*"; }
warn() { printf "${C_YELLOW}⚠ %s${C_RESET}\n" "$*"; }
err()  { printf "${C_RED}✘ %s${C_RESET}\n" "$*" >&2; }
dim()  { printf "${C_DIM}  %s${C_RESET}\n" "$*"; }

elapsed() { echo "$(( $(date +%s) - START_TS ))s"; }

# ── CLI flags ────────────────────────────────────────────────────────────────
STACKS_ARG=""
UPLOAD_ONLY=false
RESTART_ONLY=false
DRY_RUN=false
AUTO_YES=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  -s, --stacks STACKS   Comma-separated stacks to restart, or "all"
      --upload-only     Upload files only, skip restart
      --restart-only    Restart stacks only, skip upload
      --dry-run         Show what would happen without doing it
  -y, --yes             Skip upload confirmation prompt
  -h, --help            Show this help

Examples:
  $(basename "$0")                          # Interactive (default)
  $(basename "$0") -s maid-tracker,homepage # Upload + restart specific stacks
  $(basename "$0") -s all -y               # Upload + restart all (no prompt)
  $(basename "$0") --restart-only -s all   # Restart all without re-uploading
  $(basename "$0") --dry-run               # Preview rsync diff only
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--stacks)     STACKS_ARG="$2"; shift 2 ;;
    --upload-only)   UPLOAD_ONLY=true; shift ;;
    --restart-only)  RESTART_ONLY=true; shift ;;
    --dry-run)       DRY_RUN=true; shift ;;
    -y|--yes)        AUTO_YES=true; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) err "Unknown option: $1"; usage; exit 1 ;;
  esac
done

# ── Load config ──────────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  err "Config file not found: $ENV_FILE"
  echo "  Run 'make secrets' to generate it from secrets/vault.sops.yaml"
  exit 1
fi
# shellcheck source=.env
source "$ENV_FILE"

NAS_USER="${NAS_USER:?NAS_USER is not set in $ENV_FILE}"
NAS_HOST="${NAS_HOST:?NAS_HOST is not set in $ENV_FILE}"
NAS_PORT="${NAS_PORT:-2222}"
NAS_TARGET_PATH="${NAS_TARGET_PATH:-/volume1/docker}"
NAS_SSH_KEY="${NAS_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
NAS_SUDO_PASSWORD="${NAS_SUDO_PASSWORD:-}"
# Optional: set NAS_SSH_ALIAS=nas in .env to use your ssh config alias
NAS_SSH_ALIAS="${NAS_SSH_ALIAS:-}"
NAS_FILE_OWNER="${NAS_FILE_OWNER:-fixhardez}"
NAS_FILE_GROUP="${NAS_FILE_GROUP:-users}"

# ── Dependency check ─────────────────────────────────────────────────────────
for dep in ssh; do
  if ! command -v "$dep" &>/dev/null; then
    err "'$dep' is required but not installed."
    exit 1
  fi
done

# ── SSH setup — multiplexing keeps one connection alive for all commands ──────
MUX_SOCKET="/tmp/nas_deploy_mux_$$"
SSH_WRAPPER=""   # set later; referenced in trap for cleanup

if [[ -n "${NAS_SSH_ALIAS}" ]]; then
  SSH_DEST="${NAS_SSH_ALIAS}"
  SSH_BASE="-o ConnectTimeout=15"
else
  SSH_DEST="${NAS_USER}@${NAS_HOST}"
  SSH_BASE="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -p ${NAS_PORT} -i ${NAS_SSH_KEY}"
fi

# ServerAlive* lets the mux notice a TCP path silently dropped (NAT/firewall
# idle-timeout) while it sat idle waiting for the "Upload now?" answer. Without
# it, reusing the black-holed connection hangs forever (ConnectTimeout doesn't
# apply — the connection is already "established").
SSH_MUX="-o ControlMaster=auto -o ControlPath=${MUX_SOCKET} -o ControlPersist=120 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
SSH_OPTS="${SSH_BASE} ${SSH_MUX}"

trap 'ssh -o ControlPath="${MUX_SOCKET}" -O exit "${SSH_DEST}" 2>/dev/null; rm -f "${MUX_SOCKET}" "${SSH_WRAPPER:-}"' EXIT

# ── Connection check — establishes the mux master ────────────────────────────
log "Connecting to ${SSH_DEST} ..."
if ! ssh $SSH_OPTS "${SSH_DEST}" "echo OK" &>/dev/null; then
  err "Cannot connect to NAS. Check host, port, and SSH key."
  echo "  SSH key: ${NAS_SSH_KEY}"
  [[ -z "${NAS_SSH_ALIAS}" ]] && echo "  Tip: set NAS_SSH_ALIAS=nas in .env to use your ssh config alias"
  exit 1
fi
ok "Connection OK"

ALL_STACKS=(secretary n8n news-feed torrentwatch homepage jellyfin maid-tracker portainer uptime-kuma watchtower hermes-agent friendly-reminder ink-reader dupe-sweeper ops-bot shorts-factory)

# ═══════════════════════════════════════════════════════════════════════════════
# UPLOAD
# ═══════════════════════════════════════════════════════════════════════════════
if [[ "$RESTART_ONLY" == false ]]; then
  # Pre-upload: every stack with a manifest must have a rendered .env
  log "Verifying generated .env files ..."
  MISSING=()
  for stack in "${ALL_STACKS[@]}"; do
    manifest="${PROJECT_ROOT}/${stack}/secrets.manifest.yaml"
    envfile="${PROJECT_ROOT}/${stack}/.env"
    if [[ -f "$manifest" && ! -f "$envfile" ]]; then
      MISSING+=("$stack")
    fi
  done
  if [[ ${#MISSING[@]} -gt 0 ]]; then
    err "Missing .env for: ${MISSING[*]}"
    echo "  Run 'make secrets' to regenerate from secrets/vault.sops.yaml"
    exit 1
  fi
  ok "All .env files present"

  echo ""
  printf "${C_BOLD}Source :${C_RESET} %s/\n" "${PROJECT_ROOT}"
  printf "${C_BOLD}Target :${C_RESET} %s:%s\n" "${SSH_DEST}" "${NAS_TARGET_PATH}"
  echo ""

  if [[ "$DRY_RUN" == true ]]; then
    warn "DRY RUN — no files will be transferred"
    echo ""
  fi

  if [[ "$AUTO_YES" == false && "$DRY_RUN" == false ]]; then
    read -r -p "Upload now? [y/N] " CONFIRM
    [[ ! "${CONFIRM}" =~ ^[Yy]$ ]] && echo "Aborted." && exit 0
    echo ""
  fi

  # macOS ships openrsync (protocol 29) which is incompatible with Synology's
  # GNU rsync (protocol 31). Use tar+ssh instead — proven reliable across all
  # macOS versions and NAS firmware. Files are piped in a single SSH session
  # reusing the mux master established above.
  #
  # All .env files are excluded from the tar because macOS bsdtar treats
  # --exclude='./.env' as a glob that matches .env at ANY depth, not just root.
  # Instead, per-stack .env files are uploaded explicitly after the tar step.
  TAR_EXCLUDES=(
    --exclude='./.git'
    --exclude='.env'
    --exclude='./__pycache__'
    --exclude='./*.pyc'
    --exclude='./*.pyo'
    --exclude='./.DS_Store'
    --exclude='./.notes'
    --exclude='./*.egg-info'
    --exclude='./.venv'
    --exclude='./node_modules'
  )

  if [[ "$DRY_RUN" == true ]]; then
    log "DRY RUN — files that would be uploaded:"
    COPYFILE_DISABLE=1 tar -czf - "${TAR_EXCLUDES[@]}" -C "${PROJECT_ROOT}" . \
      | ssh $SSH_OPTS "${SSH_DEST}" \
        "tar -tzf - 2>/dev/null | grep -v '/$' | head -50"
    warn "Dry run complete — no files were transferred."
  else
    TMP_TAR="/tmp/nas_deploy_$$.tar.gz"

    log "Uploading project files via tar+ssh ..."
    COPYFILE_DISABLE=1 tar -czf - "${TAR_EXCLUDES[@]}" -C "${PROJECT_ROOT}" . \
      | ssh $SSH_OPTS "${SSH_DEST}" "cat > '${TMP_TAR}'"

    # Extract as root: some target dirs (e.g. uptime-kuma/, secretary/*_data) are
    # owned by root because their containers created them, so the SSH user can't
    # overwrite files there. sudo -S reads the password from the echo pipe (same
    # pattern as the restart step). mkdir/rm run as the SSH user — they touch only
    # the target root and the user-owned temp tar. No 2>/dev/null: a real failure
    # must surface, not silently kill the script via set -e. --warning=no-unknown-keyword
    # silences GNU tar's per-file noise about the com.apple.provenance xattr macOS adds.
    log "Extracting on NAS ..."
    TAR_X="tar --warning=no-unknown-keyword -xzf '${TMP_TAR}' -C '${NAS_TARGET_PATH}' --no-same-permissions --no-same-owner"
    CHOWN_X="chown -R ${NAS_FILE_OWNER}:${NAS_FILE_GROUP} '${NAS_TARGET_PATH}'"
    if [[ -n "${NAS_SUDO_PASSWORD}" ]]; then
      EXTRACT_CMD="mkdir -p '${NAS_TARGET_PATH}' && echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' ${TAR_X} && echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' ${CHOWN_X} && rm -f '${TMP_TAR}'"
    else
      warn "NAS_SUDO_PASSWORD not set — extracting without sudo (root-owned files will fail)."
      EXTRACT_CMD="mkdir -p '${NAS_TARGET_PATH}' && ${TAR_X} && ${CHOWN_X} && rm -f '${TMP_TAR}'"
    fi
    ssh $SSH_OPTS "${SSH_DEST}" "bash -lc \"${EXTRACT_CMD}\"" </dev/null

    # Per-stack .env files are excluded from the tar above — that exclude also
    # strips secrets we must NOT ship (scripts/.env, backup-pre-vault/*) — and are
    # re-uploaded selectively here. Write via a temp file + `sudo install` so
    # root-owned stack dirs (e.g. uptime-kuma/) accept the file; a plain cat as the
    # SSH user gets Permission denied there. install -D creates the dir, copies,
    # and sets mode 644 in one root call, sidestepping password/stdin contention.
    log "Uploading per-stack .env files ..."
    REMOTE_TMP_ENV="/tmp/nas_deploy_env_$$"
    for stack in "${ALL_STACKS[@]}"; do
      while IFS= read -r local_env; do
        rel_env="${local_env#${PROJECT_ROOT}/}"
        dest="${NAS_TARGET_PATH}/${rel_env}"
        ssh $SSH_OPTS "${SSH_DEST}" "cat > '${REMOTE_TMP_ENV}'" < "$local_env"
        if [[ -n "${NAS_SUDO_PASSWORD}" ]]; then
          ssh $SSH_OPTS "${SSH_DEST}" \
            "bash -lc \"echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' install -D -m 644 '${REMOTE_TMP_ENV}' '${dest}' && rm -f '${REMOTE_TMP_ENV}'\"" </dev/null
        else
          ssh $SSH_OPTS "${SSH_DEST}" \
            "bash -lc \"install -D -m 644 '${REMOTE_TMP_ENV}' '${dest}' && rm -f '${REMOTE_TMP_ENV}'\"" </dev/null
        fi
        dim "$rel_env"
      done < <(find "${PROJECT_ROOT}/${stack}" -name '.env' 2>/dev/null)
    done

    # Fix ownership: the tar extract + sudo install leave everything root-owned.
    # Restore to fixhardez:users so containers (which run as this user) can write.
    # Then fix volumes that need different ownership (n8n_data must be UID 1000:1000
    # because the n8n image runs as node:node, not fixhardez).
    log "Fixing file ownership ..."
    if [[ -n "${NAS_SUDO_PASSWORD}" ]]; then
      ssh $SSH_OPTS "${SSH_DEST}" \
        "bash -lc \"echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' chown -R ${NAS_FILE_OWNER}:${NAS_FILE_GROUP} '${NAS_TARGET_PATH}' && \
         echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' chown -R 1000:1000 '${NAS_TARGET_PATH}/n8n/n8n_data'\"" </dev/null 2>/dev/null || true
    fi

    # nginx .htpasswd files must be world-readable (644) so the nginx worker
    # process can open them. The sudo extract above leaves them root-owned 600,
    # causing a 500 Permission denied error — so the chmod must run as root too
    # (a chmod by the SSH user on a root-owned file is EPERM, silently swallowed).
    log "Fixing .htpasswd permissions ..."
    HT_SUDO=""
    [[ -n "${NAS_SUDO_PASSWORD}" ]] && HT_SUDO="echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' "
    # Single round-trip: chmod every nginx/.htpasswd under the target in one find.
    ssh $SSH_OPTS "${SSH_DEST}" \
      "bash -lc \"${HT_SUDO}find '${NAS_TARGET_PATH}' -maxdepth 3 -path '*/nginx/.htpasswd' -exec chmod 644 {} +\"" </dev/null 2>/dev/null || true

    ok "Upload complete ($(elapsed))"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# RESTART
# ═══════════════════════════════════════════════════════════════════════════════
if [[ "$UPLOAD_ONLY" == true || "$DRY_RUN" == true ]]; then
  echo ""
  ok "All done ($(elapsed))"
  exit 0
fi

if [[ -z "${NAS_SUDO_PASSWORD}" ]]; then
  echo ""
  warn "NAS_SUDO_PASSWORD not set — skipping restart (needed for docker compose)."
  echo "  Add NAS_SUDO_PASSWORD=your_password to .env to enable auto-restart."
  ok "All done ($(elapsed))"
  exit 0
fi

# ── Determine which stacks to restart ────────────────────────────────────────
STACKS_TO_RESTART=()

if [[ -n "$STACKS_ARG" ]]; then
  if [[ "$STACKS_ARG" == "all" ]]; then
    STACKS_TO_RESTART=("${ALL_STACKS[@]}")
  else
    IFS=',' read -ra _INPUT <<< "$STACKS_ARG"
    for s in "${_INPUT[@]}"; do STACKS_TO_RESTART+=("${s// /}"); done
  fi
else
  echo ""
  printf "${C_BOLD}Available stacks:${C_RESET} %s\n" "${ALL_STACKS[*]}"
  echo ""
  read -r -p "Restart stacks (comma-separated / \"all\" / Enter to skip): " STACKS_INPUT
  STACKS_INPUT="${STACKS_INPUT// /}"
  if [[ -z "$STACKS_INPUT" ]]; then
    echo "No stacks selected."
    ok "All done ($(elapsed))"
    exit 0
  elif [[ "$STACKS_INPUT" == "all" ]]; then
    STACKS_TO_RESTART=("${ALL_STACKS[@]}")
  else
    IFS=',' read -ra _INPUT <<< "$STACKS_INPUT"
    for s in "${_INPUT[@]}"; do STACKS_TO_RESTART+=("${s// /}"); done
  fi
fi

if [[ ${#STACKS_TO_RESTART[@]} -eq 0 ]]; then
  warn "No valid stacks selected."
  ok "All done ($(elapsed))"
  exit 0
fi

echo ""
log "Restarting: ${STACKS_TO_RESTART[*]}"

for stack in "${STACKS_TO_RESTART[@]}"; do
  echo ""
  printf "${C_BOLD}── %s ──${C_RESET}\n" "$stack"
  # Pass the entire command as ONE quoted string so SSH sends it verbatim to the
  # remote shell. Splitting into bash/-l/-c/cmd as separate ssh args causes the
  # remote shell to parse the pipe incorrectly (password never reaches sudo -S).
  # --project-directory makes compose resolve the stack's own .env for both
  # variable interpolation and env_file: .env inside docker-compose.yml.
  # Down first: deterministic removal of all containers managed by this compose file.
  # --remove-orphans also cleans up containers from the same project that are no
  # longer in the compose config (e.g. removed service).
  ssh $SSH_OPTS "${SSH_DEST}" \
    "bash -lc \"echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' docker compose \
      --project-directory '${NAS_TARGET_PATH}/${stack}' \
      -f '${NAS_TARGET_PATH}/${stack}/docker-compose.yml' \
      down --remove-orphans 2>&1\"" </dev/null
  # Up fresh: rebuild image and start containers.
  ssh $SSH_OPTS "${SSH_DEST}" \
    "bash -lc \"echo '${NAS_SUDO_PASSWORD}' | sudo -S -p '' docker compose \
      --project-directory '${NAS_TARGET_PATH}/${stack}' \
      -f '${NAS_TARGET_PATH}/${stack}/docker-compose.yml' \
      up -d --build 2>&1\"" </dev/null
  ok "$stack restarted"
done

echo ""
ok "All done ($(elapsed))"
