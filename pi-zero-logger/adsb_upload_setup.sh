#!/usr/bin/env bash
# adsb_upload_setup.sh - upload the Pi's aircraft to WDGoWars (signed JSON, same as the game does).
#
#   sudo bash adsb_upload_setup.sh setkey    type/paste your API key (hidden, not shown, never printed back)
#   sudo bash adsb_upload_setup.sh test      check key + connection (sends an empty signed upload)
#   sudo bash adsb_upload_setup.sh dry       show what WOULD be uploaded, send nothing
#   sudo bash adsb_upload_setup.sh upload    upload new aircraft (only ICAOs not sent before)
#   sudo bash adsb_upload_setup.sh status    counts, key file present or not (never shows the key)
#
# Needs adsb_upload.py in the same folder. Results are printed AND saved to ~/adsb_upload_result.txt
# (copy it from the terminal if you need to share it). The key lives only in
# /etc/adsb-zero/wdgwars.key (root-only). Overrides for tests: PREFIX KEY_FILE CSV STATE URL.
set -u
MODE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/adsb-zero}"
KEY_FILE="${KEY_FILE:-/etc/adsb-zero/wdgwars.key}"
CSV="${CSV:-/var/lib/adsb-zero/adsb_aircraft.csv}"
STATE="${STATE:-/var/lib/adsb-zero/uploaded_icaos.txt}"
URL="${URL:-https://wdgwars.pl/api/upload/}"
die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo:  sudo bash $0 ${MODE:-status}"
UHOME="${HOME_OVERRIDE:-$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)}"
OUT="$UHOME/adsb_upload_result.txt"
[[ -f "$HERE/adsb_upload.py" ]] || die "adsb_upload.py must be in the same folder as this script ($HERE)"
mkdir -p "$PREFIX"; install -m 755 "$HERE/adsb_upload.py" "$PREFIX/adsb_upload.py"
run() { python3 "$PREFIX/adsb_upload.py" --csv "$CSV" --state "$STATE" --key-file "$KEY_FILE" --url "$URL" "$@" 2>&1 | tee "$OUT"; r=${PIPESTATUS[0]}; echo; echo "Saved to $OUT"; return $r; }
case "$MODE" in
  setkey)
    read -rsp "Paste your WDGoWars API key (nothing will show), then press Enter: " K; echo
    K="$(printf '%s' "$K" | tr -d '[:space:]')"
    [[ ${#K} -ge 8 ]] || die "that is too short to be a key; nothing saved"
    mkdir -p "$(dirname "$KEY_FILE")"
    ( umask 177; printf '%s\n' "$K" > "$KEY_FILE" )
    chmod 600 "$KEY_FILE"; chown root:root "$KEY_FILE" 2>/dev/null
    echo "Key saved (${#K} characters) in $KEY_FILE, readable by root only."
    echo "Next:  sudo bash $0 test"
    ;;
  test)   run --test ;;
  dry)    run --dry-run ;;
  upload) run ;;
  status)
    {
      echo "key file:  $( [[ -f "$KEY_FILE" ]] && echo "present ($(stat -c '%a %U' "$KEY_FILE"))" || echo "MISSING - run setkey")"
      echo "csv rows:  $( [[ -f "$CSV" ]] && echo $(( $(wc -l < "$CSV") - 1 )) || echo "no csv")"
      echo "sent ICAOs recorded: $( [[ -f "$STATE" ]] && wc -l < "$STATE" || echo 0)"
      echo "server:    $URL"
    } | tee "$OUT"
    ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
