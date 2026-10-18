#!/usr/bin/env bash
# Runs the trading bot console on a device you keep switched on (a phone with Termux,
# a spare PC, a Raspberry Pi) and keeps it running.
#
#   * The state is saved in a file on the device (default: ~/trading-bot-state).
#   * If the app crashes or is killed, it is started again within seconds and loads the
#     last saved state: demo testing continues, trades, clock and settings are back.
#   * Optionally copies the state file to a second folder every so often (backups).
#
# Setup: copy scripts/phone.env.example to ~/.trading-bot.env, fill it in, then run
#     bash scripts/run_on_phone.sh
# Stop it with Ctrl-C (or kill the script); it then stops the app cleanly.
#
# Settings (all optional except those in the env file):
#   BOT_ENV_FILE          env file to load            (default ~/.trading-bot.env)
#   BOT_STATE_DIR         folder for the state file   (default ~/trading-bot-state)
#   BOT_BACKUP_DIR        where to keep backups       (default: no backups)
#   BOT_BACKUP_EVERY_SEC  seconds between backups     (default 3600)
#   BOT_BACKUP_KEEP       backups to keep             (default 24)
#   BOT_HOST              address to listen on        (default 127.0.0.1 = this device only;
#                         0.0.0.0 lets other devices on your Wi-Fi in; the page is plain
#                         HTTP there, so use a long ADMIN_PASS and only on a network you trust)
#   PORT                  port                        (default 8080)
#   BOT_PYTHON            python command              (default python3)
#   BOT_RESTART_DELAY     seconds before the first restart (default 5; doubles up to 60 on a crash loop)
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${BOT_ENV_FILE:-$HOME/.trading-bot.env}"

log() { printf '%s [run_on_phone] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# --- settings from the env file ---------------------------------------------------
if [ -f "$ENV_FILE" ]; then
  mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || true)"
  case "$mode" in
    ""|600|400) ;;
    *) log "WARNING: $ENV_FILE is readable by other users (mode $mode). Run: chmod 600 \"$ENV_FILE\"" ;;
  esac
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
else
  log "No env file at $ENV_FILE; using the current environment only."
fi

# --- refuse an unsafe or useless setup ----------------------------------------------
[ -n "${ADMIN_USER:-}" ] || die "ADMIN_USER is not set (put it in $ENV_FILE)."
[ -n "${ADMIN_PASS:-}" ] || die "ADMIN_PASS is not set (put it in $ENV_FILE)."
[ "${ADMIN_PASS}" != "P@ssword123!" ] || die "ADMIN_PASS is the public default from the repository. Choose your own."
[ "${#ADMIN_PASS}" -ge 10 ] || die "ADMIN_PASS must be at least 10 characters."
if [ -z "${STATE_ENCRYPTION_KEY:-}" ]; then
  log "WARNING: STATE_ENCRYPTION_KEY is not set. Everything else is saved, but API keys and the Telegram token are NOT written to disk and must be re-entered after each restart."
fi

STATE_DIR="${BOT_STATE_DIR:-$HOME/trading-bot-state}"
mkdir -p "$STATE_DIR" || die "cannot create $STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true
export DATABASE_URL="${DATABASE_URL:-$STATE_DIR/trading_bot.db}"
probe="$(dirname "$DATABASE_URL")/.write-test.$$"
{ : > "$probe"; } 2>/dev/null || die "cannot write to $(dirname "$DATABASE_URL")"
rm -f "$probe"

HOST="${BOT_HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
PYTHON="${BOT_PYTHON:-python3}"
BACKUP_DIR="${BOT_BACKUP_DIR:-}"
BACKUP_EVERY_SEC="${BOT_BACKUP_EVERY_SEC:-3600}"
BACKUP_KEEP="${BOT_BACKUP_KEEP:-24}"
export PORT

cd "$REPO_DIR" || die "cannot enter $REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON" -c "import fastapi, uvicorn, aiosqlite" 2>/dev/null \
  || die "Python packages are missing. Run: $PYTHON -m pip install -e \".[secrets]\"  (see HANDOFF.md section 6)"

# --- keep the device awake (Termux) ---------------------------------------------------
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock && log "Wake lock held."
fi

STOP=0
CHILD=""
BACKUP_LOOP=""

cleanup() {
  [ -n "$BACKUP_LOOP" ] && kill "$BACKUP_LOOP" 2>/dev/null
  command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock
  log "Stopped."
}
on_signal() {
  STOP=1
  [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null
}
trap on_signal INT TERM
trap cleanup EXIT

# --- backups --------------------------------------------------------------------------
do_backup() {
  [ -f "$DATABASE_URL" ] || return 0
  "$PYTHON" -m src.tools.backup_state backup "$DATABASE_URL" "$BACKUP_DIR" --keep "$BACKUP_KEEP" >/dev/null 2>&1 \
    && log "Backup saved to $BACKUP_DIR" \
    || log "WARNING: backup to $BACKUP_DIR failed (the bot keeps running)."
}
if [ -n "$BACKUP_DIR" ]; then
  mkdir -p "$BACKUP_DIR" 2>/dev/null || log "WARNING: cannot create $BACKUP_DIR; backups will fail until it exists."
  do_backup
  ( while sleep "$BACKUP_EVERY_SEC"; do do_backup; done ) &
  BACKUP_LOOP=$!
fi

# --- run, and run again if it dies -------------------------------------------------------
BASE_DELAY="${BOT_RESTART_DELAY:-5}"
delay=$BASE_DELAY
while [ "$STOP" -eq 0 ]; do
  log "Starting console on http://$HOST:$PORT  (state file: $DATABASE_URL)"
  started=$SECONDS
  "$PYTHON" -m uvicorn src.web.app:app --host "$HOST" --port "$PORT" --log-level warning &
  CHILD=$!
  wait "$CHILD"
  code=$?
  CHILD=""
  [ "$STOP" -eq 1 ] && break
  ran=$((SECONDS - started))
  # A crash loop (for example a broken install) must not spin: back off up to a minute.
  if [ "$ran" -ge 60 ]; then delay=$BASE_DELAY; else delay=$((delay * 2)); [ "$delay" -gt 60 ] && delay=60; fi
  log "Console exited (code $code) after ${ran}s. Restarting in ${delay}s; it will load the last saved state."
  # sleep in the background so a Ctrl-C during the pause is handled at once
  sleep "$delay" &
  wait $!
done
