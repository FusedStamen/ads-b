#!/usr/bin/env bash
# adsb_display_setup.sh - show live ADS-B status on the POD screen (tty1), or undo it.
#
#   sudo bash adsb_display_setup.sh install   put adsb_display.py in /opt/adsb-zero, start the screen service
#   sudo bash adsb_display_setup.sh colors    show 8 labelled colour bars for 20 seconds, then go back to status
#   sudo bash adsb_display_setup.sh preview   print what the screen shows, as plain text, in your terminal
#   sudo bash adsb_display_setup.sh guide top|bottom|off   move the button guide to the edge where your buttons are
#   sudo bash adsb_display_setup.sh undo      stop the service and give you the normal login prompt back
#
# Needs adsb_display.py in the same folder as this script, and the logger already installed.
# Where things go:
#   /opt/adsb-zero/adsb_display.py, /etc/systemd/system/adsb-display.service
#   Nothing is written to your boot files. The login prompt on the screen (getty@tty1) is
#   masked while the status display is installed; SSH logins are not affected.
#   When asking for help, copy the terminal output into your issue.

set -u
MODE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/adsb-zero}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
UNIT="$UNIT_DIR/adsb-display.service"
DISPLAY_ENV="${DISPLAY_ENV:-/etc/adsb-zero/display.env}"
SC() { [[ -n "${NO_SYSTEMCTL:-}" ]] || systemctl "$@"; }
SRC="$HERE/adsb_display.py"

die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo:  sudo bash $0 ${MODE:-install}"

write_unit() {
  cat > "$UNIT" <<EOF
[Unit]
Description=ADS-B status on the POD screen
After=adsb-unique-logger.service dump1090-fa.service

[Service]
Type=simple
User=adsblog
SupplementaryGroups=video
StandardInput=null
StandardOutput=tty
StandardError=journal
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
Environment=GUIDE=top
EnvironmentFile=-$DISPLAY_ENV
ExecStart=/usr/bin/python3 $PREFIX/adsb_display.py --guide \${GUIDE}
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
}

case "$MODE" in
  install)
    [[ -f "$SRC" ]] || die "adsb_display.py must be in the same folder as this script ($HERE)"
    id adsblog >/dev/null 2>&1 || die "user 'adsblog' not found: install the logger first"
    python3 -c "import ast,sys; ast.parse(open('$SRC').read())" || die "adsb_display.py does not parse"
    mkdir -p "$PREFIX"
    install -m 755 "$SRC" "$PREFIX/adsb_display.py"
    write_unit
    chmod 644 "$UNIT"
    systemctl daemon-reload
    systemctl mask getty@tty1.service >/dev/null 2>&1
    systemctl stop getty@tty1.service >/dev/null 2>&1
    systemctl enable adsb-display.service >/dev/null 2>&1
    systemctl restart adsb-display.service
    sleep 3
    echo
    systemctl is-active adsb-display.service
    echo "The screen should now show live ADS-B status."
    echo "Check colours:   sudo bash $0 colors"
    echo "See it here:     sudo bash $0 preview"
    echo "Put it back:     sudo bash $0 undo"
    ;;
  colors)
    [[ -f "$PREFIX/adsb_display.py" ]] || die "run install first"
    systemctl stop adsb-display.service
    sleep 1
    echo "Showing colour bars on the screen for 20 seconds..."
    python3 "$PREFIX/adsb_display.py" --colors --hold 20 > /dev/tty1
    systemctl start adsb-display.service
    echo "Back to the status display."
    echo "Each bar must be the colour its label says. If RED looks blue (or BLUE looks red), please report it as an issue."
    ;;
  preview)
    [[ -f "$PREFIX/adsb_display.py" ]] || die "run install first"
    python3 "$PREFIX/adsb_display.py" --plain --cols 40 --rows 15
    ;;
  guide)
    G="${2:-}"
    [[ "$G" == top || "$G" == bottom || "$G" == off ]] || die "use:  sudo bash $0 guide top   (or bottom, or off)"
    [[ -f "$UNIT" ]] || die "run install first"
    mkdir -p "$(dirname "$DISPLAY_ENV")"
    echo "GUIDE=$G" > "$DISPLAY_ENV"
    SC daemon-reload
    SC restart adsb-display.service
    echo "Button guide is now at the $G of the screen."
    ;;
  undo)
    systemctl disable --now adsb-display.service >/dev/null 2>&1
    rm -f "$UNIT"
    systemctl daemon-reload
    systemctl unmask getty@tty1.service >/dev/null 2>&1
    systemctl start getty@tty1.service >/dev/null 2>&1
    echo "Status display removed. The login prompt is back on the screen."
    echo "(/opt/adsb-zero/adsb_display.py was left in place; delete it if you like.)"
    ;;
  *)
    sed -n '2,13p' "$0"; exit 1 ;;
esac
