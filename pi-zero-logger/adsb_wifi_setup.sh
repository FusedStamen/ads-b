#!/usr/bin/env bash
# adsb_wifi_setup.sh - give the Pi several WiFi networks (home, phone hotspot, work...) and it joins
# whichever one is in range, best priority first. No screen needed.
#
# TWO WAYS TO ADD NETWORKS
#  A) From Windows with the SD card (no SSH, no Pi needed):
#     put the card in your PC, open the small drive (bootfs), edit the file  wifi-add.txt
#     (copy wifi-add.txt.example to wifi-add.txt first). One network per line:
#          SSID|PASSWORD|PRIORITY
#     Put the card back, power on. The Pi adds them at boot, then deletes the passwords from the
#     file and leaves wifi-add.done.txt (names only) so you can see what was imported.
#  B) Over SSH/PuTTY:   sudo bash adsb_wifi_setup.sh add
#
#   sudo bash adsb_wifi_setup.sh install    one-time: enables the boot-time import (way A) + wifi settings
#   sudo bash adsb_wifi_setup.sh add        add or replace one network (asks SSID, password hidden, priority)
#   sudo bash adsb_wifi_setup.sh list       saved networks and priorities (never shows passwords)
#   sudo bash adsb_wifi_setup.sh show       print each saved network WITH its password, on screen only (not saved to a file)
#   sudo bash adsb_wifi_setup.sh remove     remove one saved network
#   sudo bash adsb_wifi_setup.sh scan       what the Pi can see right now
#   sudo bash adsb_wifi_setup.sh status     connection, IP, saved networks -> saved to ~/wifi_status.txt
#   sudo bash adsb_wifi_setup.sh import     run the boot-file import now
#   sudo bash adsb_wifi_setup.sh undo       turn the boot-time import off (saved networks stay)
#
# Higher PRIORITY number wins when several are in range (home 20, phone hotspot 10, say).
# Overrides for tests: BOOT_DIR PREFIX UNIT_DIR NO_SYSTEMCTL HOME_OVERRIDE
set -u
MODE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOT_DIR="${BOOT_DIR:-/boot/firmware}"
PREFIX="${PREFIX:-/opt/adsb-zero}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SC() { [[ -n "${NO_SYSTEMCTL:-}" ]] || systemctl "$@"; }
die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo:  sudo bash $0 ${MODE:-status}"
command -v nmcli >/dev/null 2>&1 || die "nmcli (NetworkManager) not found"
UHOME="${HOME_OVERRIDE:-$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)}"
IN="$BOOT_DIR/wifi-add.txt"; DONE="$BOOT_DIR/wifi-add.done.txt"
CON_PREFIX="adsbwifi-"

add_net() {   # ssid pass prio hidden
  local ssid="$1" pass="$2" prio="${3:-10}" hidden="${4:-no}" name="${CON_PREFIX}$1"
  [[ -n "$ssid" ]] || { echo "empty SSID"; return 1; }
  [[ "$prio" =~ ^[0-9]+$ ]] || prio=10
  nmcli connection delete "$name" >/dev/null 2>&1
  local args=(connection add type wifi con-name "$name" ifname wlan0 ssid "$ssid"
              connection.autoconnect yes connection.autoconnect-priority "$prio"
              connection.autoconnect-retries 0 802-11-wireless.hidden "$hidden")
  if [[ -n "$pass" ]]; then
    args+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pass")
  fi
  local out; out="$(nmcli "${args[@]}" 2>&1)" || { echo "failed to add '$ssid': ${out//$pass/<hidden>}"; return 1; }
  echo "added '$ssid' (priority $prio$( [[ -z "$pass" ]] && echo ', open network'))"
}

do_import() {
  [[ -f "$IN" ]] || { echo "no $IN, nothing to import"; return 0; }
  local n=0 bad=0 line ssid pass prio hid
  : > "$DONE.tmp"
  echo "Imported on $(date '+%F %T') (passwords removed):" >> "$DONE.tmp"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" == \#* ]] && continue
    IFS='|' read -r ssid pass prio hid <<< "$line"
    hid="${hid:-no}"; [[ "$hid" == "hidden" || "$hid" == "yes" ]] && hid=yes || hid=no
    if r="$(add_net "$ssid" "$pass" "${prio:-10}" "$hid")"; then n=$((n+1)); echo "  OK   $r" >> "$DONE.tmp"; echo "$r"
    else bad=$((bad+1)); echo "  FAIL $ssid: ${r//$pass/<hidden>}" >> "$DONE.tmp"; echo "$r"; fi
  done < "$IN"
  mv "$DONE.tmp" "$DONE"; rm -f "$IN"; sync
  echo "Imported $n network(s), $bad failed. $IN removed so passwords do not stay on the card."
  nmcli radio wifi on >/dev/null 2>&1; nmcli device wifi rescan >/dev/null 2>&1
}

case "$MODE" in
  install)
    mkdir -p "$PREFIX"
    install -m 755 "$HERE/adsb_wifi_setup.sh" "$PREFIX/adsb_wifi_setup.sh"
    cat > "$BOOT_DIR/wifi-add.txt.example" <<'EOF'
# Copy this file to  wifi-add.txt  (same folder), edit, save, put the card back in the Pi, power on.
# One network per line:   SSID|PASSWORD|PRIORITY
#   - higher PRIORITY wins when several are in range
#   - leave PASSWORD empty for an open network
#   - add |hidden at the end for a hidden network:  MyHidden|secret|10|hidden
#   - the Pi is 2.4 GHz only. An iPhone hotspot needs  Settings > Personal Hotspot > Maximize Compatibility  ON
# Lines starting with # are ignored. After import the Pi deletes this file so passwords do not stay on the card.
HomeWifi|home-password|20
MyPhone|hotspot-password|10
EOF
    cat > "$UNIT_DIR/adsb-wifi-import.service" <<EOF
[Unit]
Description=Import WiFi networks from the boot partition (wifi-add.txt)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/bin/bash $PREFIX/adsb_wifi_setup.sh import
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$UNIT_DIR/adsb-wifi-import.service"
    SC daemon-reload; SC enable adsb-wifi-import.service >/dev/null 2>&1
    # keep wifi from sleeping (this is what the earlier powersave check was about)
    mkdir -p /etc/NetworkManager/conf.d 2>/dev/null
    printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/99-adsb-wifi.conf 2>/dev/null
    echo "Installed. On the SD card's boot drive you will find wifi-add.txt.example."
    echo "Copy it to wifi-add.txt, edit it on your PC, put the card in, power on."
    echo "Or add one now over SSH:  sudo bash $0 add"
    ;;
  add)
    while true; do
      read -rp "Network name (SSID), empty to finish: " S
      [[ -n "$S" ]] || break
      read -rsp "Password (hidden; empty for an open network): " P; echo
      read -rp "Priority (higher wins; home 20, phone hotspot 10) [10]: " PR
      read -rp "Hidden network? (y/N): " H; [[ "$H" =~ ^[Yy] ]] && H=yes || H=no
      add_net "$S" "$P" "${PR:-10}" "$H"
      echo
    done
    echo "Saved networks:"; bash "$0" list
    ;;
  list)
    echo "Saved WiFi networks (higher priority wins):"
    nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY connection show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print "  " $3 "\t" $1}' | sed "s/${CON_PREFIX}//" | sort -rn
    ;;
  show)
    echo "Saved WiFi networks with passwords (shown on screen only, not saved anywhere):"
    echo "(do not share this output)"
    echo
    while IFS= read -r name; do
      [[ -n "$name" ]] || continue
      psk="$(nmcli -e no -s -g 802-11-wireless-security.psk connection show "$name" 2>/dev/null)"
      prio="$(nmcli -e no -g connection.autoconnect-priority connection show "$name" 2>/dev/null)"
      ssid="$(nmcli -e no -g 802-11-wireless.ssid connection show "$name" 2>/dev/null)"
      printf 'priority %-3s  SSID: %s\n             password: [%s]%s\n' "${prio:-0}" "${ssid:-$name}" "${psk:-}" "$( [[ -z "$psk" ]] && echo '   (none stored: open network, or the saved login is not a simple password)')"
    done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$NF=="802-11-wireless"{sub(/:802-11-wireless$/,""); print}')
    echo
    echo "The password is between the [ ] brackets. Spaces at either end would show inside the brackets."
    ;;
  remove)
    read -rp "SSID to remove: " S
    nmcli connection delete "${CON_PREFIX}$S" 2>&1 || nmcli connection delete "$S" 2>&1
    ;;
  scan)
    nmcli device wifi rescan >/dev/null 2>&1; sleep 3
    nmcli -f IN-USE,SSID,SIGNAL,SECURITY device wifi list
    ;;
  import) do_import ;;
  status)
    {
      echo "== connection"; nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device 2>&1 | grep -E "^wlan|^eth"
      echo "== IP"; hostname -I 2>&1
      echo "== saved networks"; bash "$0" list
      echo "== boot import"; ls -la "$BOOT_DIR"/wifi-add* 2>&1
      [[ -f "$DONE" ]] && { echo "== last import"; cat "$DONE"; }
      echo "== clock"; timedatectl 2>/dev/null | grep -E "synchronized|Local time"
    } 2>&1 | tee "$UHOME/wifi_status.txt"
    echo; echo "Saved to $UHOME/wifi_status.txt"
    ;;
  undo)
    SC disable --now adsb-wifi-import.service >/dev/null 2>&1
    rm -f "$UNIT_DIR/adsb-wifi-import.service" "$BOOT_DIR/wifi-add.txt.example"; SC daemon-reload
    echo "Boot-time import removed. Saved WiFi networks were left alone."
    ;;
  *) sed -n '2,30p' "$0"; exit 1 ;;
esac
