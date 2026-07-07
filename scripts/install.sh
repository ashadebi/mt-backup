#!/usr/bin/env bash
#
# install.sh - One-command installer for MikroTik Backup Panel
# ----------------------------------------------------------------------------
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ashadebi/mt-backup/main/scripts/install.sh \
#     | sudo bash -s -- --domain backup.example.com --email admin@example.com
#
# With HTTPS (Traefik + Let's Encrypt — DNS A record must be set first):
#   curl -fsSL https://raw.githubusercontent.com/ashadebi/mt-backup/main/scripts/install.sh \
#     | sudo bash -s -- --domain backup.example.com --email admin@example.com --with-https
#
# Reset admin password (after initial install):
#   curl -fsSL https://raw.githubusercontent.com/ashadebi/mt-backup/main/scripts/install.sh \
#     | sudo bash -s -- --reset-admin
#
# What it does:
#   1. Installs Docker + Compose plugin (if missing)
#   2. Generates secrets (Fernet key, session key)
#   3. Generates a random admin password (24 chars)
#   4. Clones mt-backup repo to /opt/mt-backup
#   5. Writes data/.env with all secrets
#   6. Builds + starts the container
#   7. Optionally sets up the auto-backup cron (08:00 & 18:00 daily)
#   8. Prints admin URL + credentials to TERMINAL (not saved anywhere)
#
# ----------------------------------------------------------------------------
set -euo pipefail

# ===== Defaults ===========================================================
INSTALL_DIR="/opt/mt-backup"
PANEL_DOMAIN=""
LE_EMAIL=""
ADMIN_USER="admin"
WITH_HTTPS=0
NO_CRON=0
RESET_ADMIN=0
GITHUB_REPO="ashadebi/mt-backup"
GITHUB_BRANCH="main"

# ===== Pretty output =====================================================
if [[ -t 1 ]]; then
  BOLD=$'\e[1m'; RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'
  BLU=$'\e[34m'; CYN=$'\e[36m'; NC=$'\e[0m'
else
  BOLD=''; RED=''; GRN=''; YLW=''; BLU=''; CYN=''; NC=''
fi

log()  { printf "${BLU}▸${NC} %s\n" "$*"; }
ok()   { printf "${GRN}✓${NC} %s\n" "$*"; }
warn() { printf "${YLW}!${NC} %s\n" "$*"; }
err()  { printf "${RED}✗${NC} %s\n" "$*" >&2; }
hdr()  { printf "\n${BOLD}${CYN}── %s ──${NC}\n" "$*"; }

# ===== Args ===============================================================
usage() {
  cat <<EOF
${BOLD}mt-backup installer${NC}

${BOLD}Usage:${NC}
  sudo $0 --domain HOSTNAME --email EMAIL [options]
  sudo $0 --reset-admin

${BOLD}Required:${NC}
  --domain HOSTNAME     Domain or subdomain for the panel (e.g. backup.example.com)
  --email EMAIL         Email for Let's Encrypt registration (any valid email)

${BOLD}Options:${NC}
  --with-https          Include Traefik + Let's Encrypt (HTTPS auto-cert).
                        Requires DNS A record pointed to this server.
  --no-cron             Skip the auto-backup cron setup (08:00 & 18:00 daily)
  --reset-admin         Reset admin password (must be installed first)
  --install-dir PATH    Install location (default: /opt/mt-backup)
  --github-repo OWNER/REPO   Override GitHub repo (default: ashadebi/mt-backup)
  --github-branch BRANCH     Override branch (default: main)
  -h, --help            Show this help

${BOLD}Examples:${NC}
  # Standalone (port 8000, HTTP only — for testing)
  sudo $0 --domain backup.example.com --email admin@example.com

  # Production (HTTPS via Traefik + LE)
  sudo $0 --domain backup.example.com --email admin@example.com --with-https

  # Reset admin password (will be printed once)
  sudo $0 --reset-admin
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)        PANEL_DOMAIN="$2"; shift 2 ;;
    --email)         LE_EMAIL="$2"; shift 2 ;;
    --with-https)    WITH_HTTPS=1; shift ;;
    --no-cron)       NO_CRON=1; shift ;;
    --reset-admin)   RESET_ADMIN=1; shift ;;
    --install-dir)   INSTALL_DIR="$2"; shift 2 ;;
    --github-repo)   GITHUB_REPO="$2"; shift 2 ;;
    --github-branch) GITHUB_BRANCH="$2"; shift 2 ;;
    -h|--help)       usage ;;
    *)               err "Unknown arg: $1"; usage ;;
  esac
done

# ===== Print credentials (TERMINAL ONLY — not saved) ====================
print_credentials() {
  local url_scheme="http"
  local url_port="8000"
  if [[ $WITH_HTTPS -eq 1 ]]; then
    url_scheme="https"
    url_port=""
  fi
  local url_display
  if [[ -n "$url_port" ]]; then
    url_display="${url_scheme}://${PANEL_DOMAIN}:${url_port}/"
  else
    url_display="${url_scheme}://${PANEL_DOMAIN}/"
  fi

  hdr "════════════════════════════════════════════════════════════"

  cat <<EOF

${BOLD}${GRN}  mt-backup is installed and running!${NC}

${BOLD}  ${CYN}Panel URL${NC}       ${BOLD}${url_display}${NC}
${BOLD}  ${CYN}Server IP${NC}       ${SERVER_IP}

${BOLD}${YLW}  ┌─────────────────────────────────────────────────────┐${NC}
${BOLD}${YLW}  │  ${RED}ADMIN CREDENTIALS — WRITE THIS DOWN NOW${YLW}            │${NC}
${BOLD}${YLW}  │${NC}
${BOLD}${YLW}  │${NC}   ${CYN}Username${NC}   ${BOLD}${ADMIN_USER}${NC}
${BOLD}${YLW}  │${NC}   ${CYN}Password${NC}   ${BOLD}${RED}${ADMIN_PASSWORD}${NC}
${BOLD}${YLW}  │${NC}
${BOLD}${YLW}  └─────────────────────────────────────────────────────┘${NC}

EOF

  cat <<EOF
${YLW}  ⚠  The password is NOT saved anywhere.${NC}
${YLW}     If you lose it, re-run this script with --reset-admin${NC}
${YLW}     or SSH to this host and reset via the database.${NC}

${BOLD}  Useful commands:${NC}
    ${CYN}View logs:${NC}        docker logs -f mt-backup
    ${CYN}Restart:${NC}          docker restart mt-backup
    ${CYN}Stop:${NC}             cd $INSTALL_DIR && docker compose -f $COMPOSE_FILE stop
    ${CYN}Update:${NC}           cd $INSTALL_DIR && git pull && docker compose -f $COMPOSE_FILE up -d --build
    ${CYN}Manual backup:${NC}    docker exec mt-backup python3 /app/scripts/backup.py
    ${CYN}Config file:${NC}      nano $INSTALL_DIR/data/.env
    ${CYN}Add a router:${NC}     Login → Routers → ➕ Tambah Router

${BOLD}  Next steps:${NC}
    1. ${BOLD}Open the URL above${NC} in your browser
    2. Login with the credentials shown
    3. Go to ${BOLD}Routers → ➕ Tambah Router${NC}
    4. Enter MikroTik IP + SSH credentials
    5. The panel auto-detects name, model, location
    6. Click ${BOLD}🔌 Test Connection${NC} then ${BOLD}▶ Backup Now${NC}

${BOLD}  Cron schedule:${NC}  $( [[ $NO_CRON -eq 0 ]] && echo "Auto-backup at 08:00 & 18:00 daily (configured)" || echo "Skipped (--no-cron)" )

EOF

  printf "${YLW}  ╭──────────────────────────────────────────────────────╮${NC}\n"
  printf "${YLW}  │${NC}  ${BOLD}SAVE YOUR CREDENTIALS NOW${NC}                            ${YLW}│${NC}\n"
  printf "${YLW}  │${NC}  Scroll up to see them — they won't be shown again.  ${YLW}│${NC}\n"
  printf "${YLW}  ╰──────────────────────────────────────────────────────╯${NC}\n\n"
}

# ===== Pre-flight ========================================================
hdr "Pre-flight checks"

if [[ $EUID -ne 0 ]]; then
  err "Must run as root. Re-run with: sudo $0 ..."
  exit 1
fi

if [[ $RESET_ADMIN -eq 0 ]] && { [[ -z "$PANEL_DOMAIN" ]] || [[ -z "$LE_EMAIL" ]]; }; then
  err "Both --domain and --email are required (or use --reset-admin)."
  echo ""
  usage
fi

if [[ -n "$PANEL_DOMAIN" ]] && [[ "$PANEL_DOMAIN" == *"*"* ]]; then
  err "Wildcard domain not supported. Use a specific hostname."
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  log "Installing git..."
  apt-get update -qq
  apt-get install -y -qq git
fi

if ! command -v curl >/dev/null 2>&1; then
  err "curl is required. Install with: apt-get install -y curl"
  exit 1
fi

# Install Python deps for secret generation (bcrypt + cryptography)
if ! python3 -c "import bcrypt, cryptography" 2>/dev/null; then
  log "Installing python3-bcrypt and python3-cryptography..."
  apt-get update -qq
  apt-get install -y -qq python3-bcrypt python3-cryptography
fi

ok "Running as root on $(. /etc/os-release && echo "$PRETTY_NAME")"

# ===== Reset mode (early exit after printing) ============================
if [[ $RESET_ADMIN -eq 1 ]]; then
  hdr "Reset admin password"
  cd "$INSTALL_DIR" || { err "Not installed yet — run full install first."; exit 1; }
  if [[ ! -f data/.env ]]; then
    err "data/.env not found at $INSTALL_DIR/data/.env. Run full install first."
    exit 1
  fi
  # Load existing env
  ADMIN_USER=$(grep -E '^MT_ADMIN_USERNAME=' data/.env | cut -d= -f2)
  ADMIN_USER="${ADMIN_USER:-admin}"
  PANEL_DOMAIN=$(grep -E '^MT_PANEL_DOMAIN=' data/.env | cut -d= -f2)
  PANEL_DOMAIN="${PANEL_DOMAIN:-your-server}"
  SERVER_IP=$(ip -4 route get 1 2>/dev/null | awk '{print $7; exit}')
  [[ -z "$SERVER_IP" ]] && SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  # Generate new password
  ADMIN_PASSWORD=$(python3 -c "import secrets, string; chars = string.ascii_letters + string.digits + '-_'; print(''.join(secrets.choice(chars) for _ in range(24)))")
  ADMIN_HASH=$(python3 -c "
import bcrypt
print(bcrypt.hashpw(b'${ADMIN_PASSWORD}', bcrypt.gensalt(rounds=12)).decode())
")
  sed -i "s|^MT_ADMIN_PASSWORD_HASH=.*|MT_ADMIN_PASSWORD_HASH=${ADMIN_HASH}|" data/.env
  sed -i "s|^MT_ADMIN_USERNAME=.*|MT_ADMIN_USERNAME=${ADMIN_USER}|" data/.env
  chmod 600 data/.env
  ok "Admin password regenerated. Restarting container..."
  if [[ -f docker-compose.https.yml ]] && grep -q "MT_PANEL_DOMAIN" docker-compose.https.yml 2>/dev/null; then
    COMPOSE_FILE="docker-compose.https.yml"
  else
    COMPOSE_FILE="docker-compose.simple.yml"
  fi
  docker compose -f "$COMPOSE_FILE" restart mt-backup 2>/dev/null || docker restart mt-backup
  ok "Container restarted"
  # Determine URL
  WITH_HTTPS=0
  [[ -f docker-compose.https.yml ]] && grep -q "mt-backup-traefik" docker-compose.https.yml 2>/dev/null && WITH_HTTPS=1
  print_credentials
  exit 0
fi

# ===== Install Docker ====================================================
hdr "Installing Docker (if missing)"

if ! command -v docker >/dev/null 2>&1; then
  . /etc/os-release
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  ok "Docker installed: $(docker --version)"
else
  ok "Docker already installed: $(docker --version | awk '{print $3}')"
fi

if ! docker compose version >/dev/null 2>&1; then
  err "Docker Compose plugin missing. Install: apt-get install docker-compose-plugin"
  exit 1
fi
ok "Docker Compose: $(docker compose version --short)"

# ===== Detect server IP =================================================
SERVER_IP=$(ip -4 route get 1 2>/dev/null | awk '{print $7; exit}')
[[ -z "$SERVER_IP" ]] && SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[[ -z "$SERVER_IP" ]] && SERVER_IP="your-server"

# ===== Clone / update repo ===============================================
hdr "Cloning mt-backup from GitHub"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  log "Repo already at $INSTALL_DIR — pulling latest"
  cd "$INSTALL_DIR"
  git pull origin "$GITHUB_BRANCH" --ff-only 2>&1 | tail -2 || warn "Pull failed, continuing with existing"
else
  if [[ -d "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    err "$INSTALL_DIR exists and is not empty (no .git). Move it aside or remove it."
    exit 1
  fi
  git clone --depth 1 --branch "$GITHUB_BRANCH" "https://github.com/$GITHUB_REPO.git" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
  ok "Cloned to $INSTALL_DIR"
fi

# ===== Generate secrets ==================================================
hdr "Generating secrets"

# Admin password — random 24 chars (alphanumeric + safe symbols)
ADMIN_PASSWORD=$(python3 -c "import secrets, string; chars = string.ascii_letters + string.digits + '-_'; print(''.join(secrets.choice(chars) for _ in range(24)))")

# Fernet key for SSH password encryption
FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Session secret
SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# Cron token (random 32 chars)
CRON_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")

# Bcrypt hash of admin password (rounds=12)
ADMIN_HASH=$(python3 -c "
import bcrypt
print(bcrypt.hashpw(b'${ADMIN_PASSWORD}', bcrypt.gensalt(rounds=12)).decode())
")

ok "Admin password generated (24 chars)"
ok "Fernet key generated (44 chars base64)"
ok "Session secret generated (64 chars hex)"
ok "Bcrypt hash computed (rounds=12)"

# ===== Write .env ========================================================
hdr "Writing data/.env"

mkdir -p data backups
chmod 700 data backups

cat > data/.env <<ENVEOF
# mt-backup generated by install.sh — $(date -u +'%Y-%m-%dT%H:%M:%SZ')
# DO NOT COMMIT. Mode 600.

MT_FERNET_KEY=${FERNET_KEY}
MT_SECRET_KEY=${SESSION_SECRET}
MT_ADMIN_USERNAME=${ADMIN_USER}
MT_ADMIN_PASSWORD_HASH=${ADMIN_HASH}
MT_CRON_TOKEN=${CRON_TOKEN}

MT_PANEL_DOMAIN=${PANEL_DOMAIN}
MT_DATA_DIR=/app/data
MT_BACKUP_DIR=/app/backups
ENVEOF
chmod 600 data/.env
ok "data/.env written (mode 600)"

# ===== Build + start container ==========================================
hdr "Building & starting container"

if [[ $WITH_HTTPS -eq 1 ]]; then
  COMPOSE_FILE="docker-compose.https.yml"
else
  COMPOSE_FILE="docker-compose.simple.yml"
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  err "Compose file not found: $COMPOSE_FILE"
  exit 1
fi

# Pull/build
docker compose -f "$COMPOSE_FILE" build 2>&1 | tail -5 || {
  err "Build failed. Check: docker compose -f $COMPOSE_FILE build"
  exit 1
}

# Start
docker compose -f "$COMPOSE_FILE" up -d 2>&1 | tail -5
ok "Container started"

# ===== Wait for healthcheck =============================================
hdr "Waiting for panel to come up"

for i in {1..30}; do
  sleep 2
  if [[ $WITH_HTTPS -eq 1 ]]; then
    PROBE_URL="http://127.0.0.1/healthz"
  else
    PROBE_URL="http://127.0.0.1:8000/healthz"
  fi
  if curl -fsS --max-time 3 "$PROBE_URL" >/dev/null 2>&1; then
    ok "Panel responding on $PROBE_URL (after ${i} attempts)"
    break
  fi
  printf "."
done
echo ""

if ! curl -fsS --max-time 3 "$PROBE_URL" >/dev/null 2>&1; then
  warn "Panel not responding yet. Check logs: docker compose -f $COMPOSE_FILE logs"
fi

# ===== Setup cron (optional) ============================================
if [[ $NO_CRON -eq 0 ]]; then
  hdr "Setting up auto-backup cron (08:00 & 18:00 daily)"
  cat > /etc/cron.d/mt-backup <<CRONEOF
# MikroTik Backup Panel — auto-backup at 08:00 and 18:00 daily
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

0 8,18 * * * root docker exec mt-backup python3 /app/scripts/backup.py >> /var/log/mt-backup-cron.log 2>&1
CRONEOF
  chmod 644 /etc/cron.d/mt-backup
  if systemctl reload cron 2>/dev/null || systemctl reload crond 2>/dev/null; then
    ok "Cron installed (/etc/cron.d/mt-backup)"
  else
    warn "Cron file written but reload failed. Check: systemctl status cron"
  fi
fi

# ===== Final: print credentials =========================================
print_credentials