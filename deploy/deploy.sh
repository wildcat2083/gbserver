#!/usr/bin/env bash
# deploy.sh - one-shot deploy/upgrade for gbserver on the Pi.
#
# Usage (as the service user, NOT root):
#   bash deploy-gbserver.sh            # uses ~/gbserver.zip if present, else git pull
#   bash deploy-gbserver.sh --git      # force git pull
#   bash deploy-gbserver.sh --zip PATH # use a specific zip
#
# What it does: backs everything up, updates the code, creates
# /etc/gbserver.env with a strong admin token, installs the systemd unit,
# scoped sudoers rule and nginx config, restarts, runs health/security
# checks, and rolls EVERYTHING back automatically if any step fails.

set -Eeuo pipefail
export PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"

# ---- settings -------------------------------------------------------------
APP_USER="$(id -un)"
APP_DIR="${APP_DIR:-$HOME/gbserver}"
SERVICE="gbserver"
PUBLIC_HOST="gbserver.wulfpax-labs.com"
INTERNAL_HOST="gbserver-internal.wulfpax-labs.com"
UNIT_DST="/etc/systemd/system/${SERVICE}.service"
ENV_DST="/etc/gbserver.env"
SUDOERS_DST="/etc/sudoers.d/gbserver-certcheck"
NGINX_SITE="/etc/nginx/sites-available/gbserver.conf"
NGINX_LINK="/etc/nginx/sites-enabled/gbserver.conf"
NGINX_SNIPPET="/etc/nginx/snippets/gbserver-locations.conf"
PUBLIC_CERT="/etc/letsencrypt/live/${PUBLIC_HOST}/cert.pem"
INTERNAL_CERT="/etc/nginx/certs/gbserver.crt"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/gbserver-backups/$STAMP"
# ---------------------------------------------------------------------------

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_off=$'\e[0m'
step() { printf '\n%s==>%s %s\n' "$c_ok" "$c_off" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    %s[warn]%s %s\n' "$c_warn" "$c_off" "$*"; WARNINGS+=("$*"); }
die()  { printf '\n%s[error]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
WARNINGS=()

MODE=""; ZIP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --git) MODE=git; shift ;;
    --zip) MODE=zip; ZIP="${2:-}"; shift 2 ;;
    -h|--help) sed -n 2,14p "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ $EUID -ne 0 ]] || die "run this as your normal user (e.g. luna), not root - it uses sudo itself"
[[ -d "$APP_DIR" ]] || die "$APP_DIR not found (set APP_DIR=/path if it lives elsewhere)"

if [[ -z "$MODE" ]]; then
  for z in "$HOME/gbserver.zip" "$(dirname "$(readlink -f "$0")")/gbserver.zip"; do
    [[ -f "$z" ]] && { MODE=zip; ZIP="$z"; break; }
  done
  [[ -n "$MODE" ]] || MODE=git
fi
[[ "$MODE" != zip || -f "$ZIP" ]] || die "zip not found: $ZIP"

step "Checking sudo"
sudo -v || die "sudo is required"
# keep sudo alive for the whole run
( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &
SUDO_KEEPALIVE=$!

for cmd in curl rsync unzip openssl nginx systemctl visudo; do
  command -v "$cmd" >/dev/null 2>&1 || {
    [[ "$cmd" == rsync || "$cmd" == unzip ]] && { sudo apt-get install -y -qq "$cmd" >/dev/null; continue; }
    die "required command missing: $cmd"
  }
done


# ---- backup ----------------------------------------------------------------
step "Backing up to $BACKUP"
mkdir -p "$BACKUP/app" "$BACKUP/sys"
rsync -a --exclude venv/ --exclude roms/ --exclude saves/ --exclude __pycache__/ "$APP_DIR/" "$BACKUP/app/"
declare -A HAD
for f in "$UNIT_DST" "$ENV_DST" "$SUDOERS_DST" "$NGINX_SITE" "$NGINX_SNIPPET" "$NGINX_LINK"; do
  key="$(echo "$f" | tr / _)"
  if sudo test -e "$f"; then
    HAD[$key]=1
    sudo cp -a "$f" "$BACKUP/sys/$key"
  else
    HAD[$key]=0
  fi
done
OLD_GIT_HEAD=""
if git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  OLD_GIT_HEAD="$(git -C "$APP_DIR" rev-parse HEAD)"
fi
info "done (code, systemd unit, env, sudoers, nginx)"

ROLLING_BACK=0
rollback() {
  local rc=$?
  [[ $ROLLING_BACK -eq 0 ]] || return
  ROLLING_BACK=1
  trap - ERR
  set +e
  printf '\n%s[error]%s deploy failed (line %s) - rolling back\n' "$c_err" "$c_off" "${1:-?}" >&2
  if [[ -n "$OLD_GIT_HEAD" && "$MODE" == git ]]; then
    git -C "$APP_DIR" reset -q --hard "$OLD_GIT_HEAD"
  fi
  rsync -a "$BACKUP/app/" "$APP_DIR/"
  for f in "$UNIT_DST" "$ENV_DST" "$SUDOERS_DST" "$NGINX_SITE" "$NGINX_SNIPPET" "$NGINX_LINK"; do
    key="$(echo "$f" | tr / _)"
    if [[ "${HAD[$key]:-0}" == 1 ]]; then
      sudo cp -a "$BACKUP/sys/$key" "$f"
    else
      sudo rm -f "$f"
    fi
  done
  sudo systemctl daemon-reload
  sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx
  sudo systemctl restart "$SERVICE"
  kill "$SUDO_KEEPALIVE" 2>/dev/null
  printf '%s[rolled back]%s previous version restored. Backup kept at %s\n' "$c_warn" "$c_off" "$BACKUP" >&2
  exit "${rc:-1}"
}
trap 'rollback $LINENO' ERR
fail() { printf '    %s[fail]%s %s\n' "$c_err" "$c_off" "$*" >&2; false; }

# ---- code ------------------------------------------------------------------
step "Updating code ($MODE)"
if [[ "$MODE" == zip ]]; then
  TMP="$(mktemp -d)"
  unzip -q "$ZIP" -d "$TMP"
  SRC="$TMP/gbserver"
  [[ -f "$SRC/app.py" ]] || { rm -rf "$TMP"; fail "zip doesn't contain gbserver/app.py"; }
  rsync -a --exclude .git/ --exclude venv/ --exclude roms/ --exclude saves/ \
        --exclude blocked_ips.json --exclude offline.flag "$SRC/" "$APP_DIR/"
  rm -rf "$TMP"
  info "extracted $ZIP over $APP_DIR (ROMs, saves, blocklist untouched)"
else
  git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1 || fail "$APP_DIR is not a git repo - copy gbserver.zip to ~ and rerun"
  if [[ -n "$(git -C "$APP_DIR" status --porcelain --untracked-files=no)" ]]; then
    git -C "$APP_DIR" stash push -q -m "deploy.sh $STAMP"
    warn "local changes were stashed (git stash list)"
  fi
  git -C "$APP_DIR" pull -q --ff-only
  info "now at $(git -C "$APP_DIR" log -1 --format='%h %s')"
fi
# the security commit untracks blocked_ips.json, which deletes it on pull
if [[ ! -f "$APP_DIR/blocked_ips.json" && -f "$BACKUP/app/blocked_ips.json" ]]; then
  cp "$BACKUP/app/blocked_ips.json" "$APP_DIR/blocked_ips.json"
  info "restored blocked_ips.json"
fi
grep -q "def safe_rom_path" "$APP_DIR/config.py" || fail "code doesn't contain the path-traversal fix - is GitHub up to date? (use the zip instead)"
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +

step "Python dependencies"
[[ -x "$APP_DIR/venv/bin/pip" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
info "ok"

# ---- admin token -----------------------------------------------------------
step "Admin token ($ENV_DST)"
token_ok() { local t="$1"; [[ ${#t} -ge 32 ]] && [[ ! "${t,,}" =~ ^(changeme|change-me|admin|password)$ ]]; }
EXISTING_TOKEN=""
if sudo test -f "$ENV_DST"; then
  EXISTING_TOKEN="$(sudo sed -n 's/^GBSERVER_ADMIN_TOKEN=//p' "$ENV_DST" | tail -1 | tr -d '\r"'"'"' ')"
fi
if [[ -z "$EXISTING_TOKEN" && -f "$BACKUP/sys/$(echo "$UNIT_DST" | tr / _)" ]]; then
  EXISTING_TOKEN="$(sudo sed -n 's/^Environment=GBSERVER_ADMIN_TOKEN=//p' "$BACKUP/sys/$(echo "$UNIT_DST" | tr / _)" | tail -1 | tr -d '\r"'"'"' ')"
fi
NEW_TOKEN=""
if token_ok "$EXISTING_TOKEN"; then
  TOKEN="$EXISTING_TOKEN"
  info "keeping your existing strong token"
else
  TOKEN="$(openssl rand -hex 32)"
  NEW_TOKEN="$TOKEN"
  [[ -n "$EXISTING_TOKEN" ]] && info "old token was a placeholder/too short - generated a new one"
fi
TMPENV="$(mktemp)"
if sudo test -f "$ENV_DST"; then
  sudo grep -v '^GBSERVER_ADMIN_TOKEN=' "$ENV_DST" > "$TMPENV" || true
else
  grep -v '^GBSERVER_ADMIN_TOKEN=' "$APP_DIR/deploy/systemd/gbserver.env.example" > "$TMPENV" || true
fi
printf 'GBSERVER_ADMIN_TOKEN=%s\n' "$TOKEN" >> "$TMPENV"
sudo install -m 600 -o root -g root "$TMPENV" "$ENV_DST"
rm -f "$TMPENV"
info "written (mode 600, root-owned)"

# ---- systemd ---------------------------------------------------------------
step "systemd unit"
TMPUNIT="$(mktemp)"
sed -e "s|^User=.*|User=${APP_USER}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${APP_DIR}|" \
    -e "s|^ExecStart=.*|ExecStart=${APP_DIR}/venv/bin/gunicorn -c gunicorn.conf.py app:app|" \
    "$APP_DIR/deploy/systemd/gbserver.service" > "$TMPUNIT"
OLD_UNIT="$BACKUP/sys/$(echo "$UNIT_DST" | tr / _)"
if [[ -f "$OLD_UNIT" ]]; then
  # carry over any extra Environment= lines you added (never the token)
  while IFS= read -r line; do
    line="${line%$'\r'}"
    key="${line#Environment=}"; key="${key%%=*}"
    [[ "$key" == GBSERVER_ADMIN_TOKEN ]] && continue
    grep -q "^Environment=${key}=" "$TMPUNIT" && continue
    L="$line" awk '/^EnvironmentFile=/{print ENVIRON["L"]}1' "$TMPUNIT" > "$TMPUNIT.new" && mv "$TMPUNIT.new" "$TMPUNIT"
    info "kept your custom setting: $key"
  done < <(sudo grep '^Environment=' "$OLD_UNIT" || true)
fi
sudo install -m 644 -o root -g root "$TMPUNIT" "$UNIT_DST"
rm -f "$TMPUNIT"
sudo systemctl daemon-reload
sudo systemctl enable -q "$SERVICE"
info "installed"

# ---- sudoers ---------------------------------------------------------------
step "Scoped sudoers rule for the dashboard's cert panel"
TMPSUDO="$(mktemp)"
{
  echo "# Managed by gbserver deploy.sh - lets the dashboard read cert expiry, nothing else"
  echo "${APP_USER} ALL=(root) NOPASSWD: /usr/bin/openssl x509 -enddate -noout -in ${PUBLIC_CERT}"
  echo "${APP_USER} ALL=(root) NOPASSWD: /usr/bin/openssl x509 -enddate -noout -in ${INTERNAL_CERT}"
} > "$TMPSUDO"
sudo visudo -cqf "$TMPSUDO" || { rm -f "$TMPSUDO"; fail "sudoers syntax check failed"; }
sudo install -m 440 -o root -g root "$TMPSUDO" "$SUDOERS_DST"
rm -f "$TMPSUDO"
info "installed"
BROAD="$(sudo grep -rnE 'openssl' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | grep -v "^${SUDOERS_DST}:" | grep -v 'x509 -enddate' | grep -v '^\s*#' || true)"
if [[ -n "$BROAD" ]]; then
  warn "a broader openssl sudo rule exists and should be removed by hand (it can read any file):"
  printf '      %s\n' "$BROAD"
fi

# ---- nginx -----------------------------------------------------------------
step "nginx"
OTHER="$(sudo grep -rlE '^\s*upstream\s+gbserver\b' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | while read -r f; do [[ "$(readlink -f "$f")" == "$(readlink -f "$NGINX_SITE")" ]] || echo "$f"; done || true)"
[[ -z "$OTHER" ]] || fail "another enabled nginx file also defines gbserver (remove/disable it first): $OTHER"
sudo mkdir -p "$(dirname "$NGINX_SNIPPET")"
sudo install -m 644 -o root -g root "$APP_DIR/deploy/nginx/deploy_gbserver.conf" "$NGINX_SITE"
sudo install -m 644 -o root -g root "$APP_DIR/deploy/nginx/deploy_gbserver-locations.conf" "$NGINX_SNIPPET"
sudo ln -sfn "$NGINX_SITE" "$NGINX_LINK"
if ! sudo nginx -t 2>/tmp/gbserver-nginx-test.log; then
  cat /tmp/gbserver-nginx-test.log >&2
  fail "nginx config test failed"
fi
sudo systemctl reload nginx
info "config valid, reloaded"

# ---- restart ---------------------------------------------------------------
step "Restarting $SERVICE"
RESTART_TS="$(date '+%Y-%m-%d %H:%M:%S')"
sudo systemctl restart "$SERVICE"
for i in $(seq 1 30); do
  curl -s -o /dev/null http://127.0.0.1:8080/api/config && break
  sleep 1
  [[ $i -eq 30 ]] && { sudo journalctl -u "$SERVICE" -n 40 --no-pager >&2; fail "$SERVICE didn't come up within 30s"; }
done
info "up"
sudo journalctl -u "$SERVICE" --since "$RESTART_TS" --no-pager > /tmp/gbserver-journal.log 2>&1 || true
if grep -q 'GBSERVER_ADMIN_TOKEN' /tmp/gbserver-journal.log; then
  fail "service reports the admin token as missing/weak - check $ENV_DST"
fi

# ---- verification ----------------------------------------------------------
step "Verifying"
code() { curl -sk -m 10 -o /dev/null -w '%{http_code}' "$@" || true; }
PUB=(--resolve "${PUBLIC_HOST}:443:127.0.0.1")
INT=(--resolve "${INTERNAL_HOST}:443:127.0.0.1")
check() { # name expected actual
  if [[ "$3" == "$2" ]]; then info "${c_ok}pass${c_off}  $1"; else fail "$1 (expected $2, got $3)"; fi
}

check "public: ROM upload refused"          403 "$(code "${PUB[@]}" -X POST "https://${PUBLIC_HOST}/api/upload")"
check "public: room ROM delete refused"     403 "$(code "${PUB[@]}" -X DELETE "https://${PUBLIC_HOST}/r/ABCDEF/api/rom/x.gb")"
check "public: dashboard hidden"            404 "$(code "${PUB[@]}" "https://${PUBLIC_HOST}/dashboard")"
check "public: admin API hidden"            404 "$(code "${PUB[@]}" "https://${PUBLIC_HOST}/api/admin/action-log")"
if [[ -f "$APP_DIR/offline.flag" ]]; then
  warn "server is in offline mode - skipped player-side checks"
else
  check "public: player page loads"         200 "$(code "${PUB[@]}" "https://${PUBLIC_HOST}/")"
  check "internal: upload reaches app"      400 "$(code "${INT[@]}" -X POST "https://${INTERNAL_HOST}/api/upload")"
fi
check "internal: admin token accepted"      200 "$(code "${INT[@]}" -H "Authorization: Bearer ${TOKEN}" "https://${INTERNAL_HOST}/api/admin/action-log")"
check "internal: path traversal blocked"    400 "$(code "${INT[@]}" -X DELETE -H "Authorization: Bearer ${TOKEN}" "https://${INTERNAL_HOST}/api/admin/rom/..%2Fapp.py")"
[[ -f "$APP_DIR/app.py" ]] && info "${c_ok}pass${c_off}  app.py still present" || fail "app.py missing"

trap - ERR
kill "$SUDO_KEEPALIVE" 2>/dev/null || true

# drop cached sudo creds so this only passes via the NOPASSWD rule itself
sudo -k
if sudo -n /usr/bin/openssl x509 -enddate -noout -in "$INTERNAL_CERT" >/dev/null 2>&1; then
  info "${c_ok}pass${c_off}  cert-expiry sudo rule works"
else
  warn "cert-expiry check can't read $INTERNAL_CERT - the dashboard cert panel will show an error"
fi

# ---- summary ---------------------------------------------------------------
step "Deploy complete"
info "backup: $BACKUP  (safe to delete once you're happy)"
if [[ -n "$NEW_TOKEN" ]]; then
  printf '\n    %sNEW ADMIN TOKEN%s - save it in your password manager, it is shown only once:\n\n      %s\n\n' "$c_warn" "$c_off" "$NEW_TOKEN"
  info "(it's also readable later with: sudo cat $ENV_DST)"
fi
if [[ ${#WARNINGS[@]} -gt 0 ]]; then
  printf '\n    %s%d warning(s) above to look at.%s\n' "$c_warn" "${#WARNINGS[@]}" "$c_off"
fi
