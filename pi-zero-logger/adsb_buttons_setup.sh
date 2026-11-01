#!/usr/bin/env bash
# adsb_buttons_setup.sh - turn on the four Argon POD buttons (and the two-page screen that goes with them).
#
#   sudo bash adsb_buttons_setup.sh install   install the button service; also updates the screen program
#   sudo bash adsb_buttons_setup.sh test      stop the service and show live button presses for 20 seconds
#   sudo bash adsb_buttons_setup.sh status    what is installed and the last log lines -> ~/buttons_status.txt
#   sudo bash adsb_buttons_setup.sh undo      remove the button service (the screen keeps working)
#
# Buttons (left to right on the case; GPIO 16, 20, 21, 26 per the Argon POD manual):
#   1  short press   switch screen page: ADS-B stats / network (WiFi name, IP, clock, bias tee)
#   2  HOLD 3 s      toggle the RTL-SDR V4 bias tee (needs adsb_biast_setup.sh installed first)
#   3  HOLD 3 s      safe shutdown, copies logs to the SD card first, then power off
#   4  short press   copy the logs to the SD card (boot partition) right now
# The screen shows a progress bar while you hold, then a message. Needs adsb_buttons.py and
# adsb_display.py in the same folder as this script, and the screen service installed.
# Where things go: /opt/adsb-zero/adsb_buttons.py, /etc/systemd/system/adsb-buttons.service.
# Test output is printed AND saved to ~/buttons_test.txt (copy it from the terminal when asking for help).
# Overrides for tests: PREFIX UNIT_DIR NO_SYSTEMCTL HOME_OVERRIDE GPIOMEM
set -u
MODE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/adsb-zero}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
UNIT="$UNIT_DIR/adsb-buttons.service"
GPIOMEM="${GPIOMEM:-/dev/gpiomem}"
SC() { [[ -n "${NO_SYSTEMCTL:-}" ]] || systemctl "$@"; }
die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo:  sudo bash $0 ${MODE:-status}"
UHOME="${HOME_OVERRIDE:-$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)}"

case "$MODE" in
  install)
    [[ -f "$HERE/adsb_buttons.py" ]] || die "adsb_buttons.py must be in the same folder as this script ($HERE)"
    [[ -f "$HERE/adsb_display.py" ]] || die "adsb_display.py (the new version) must be in the same folder too"
    python3 -c "import ast;[ast.parse(open('$HERE/'+f).read()) for f in ('adsb_buttons.py','adsb_display.py')]" || die "a program file does not parse"
    [[ -e "$GPIOMEM" ]] || echo "WARNING: $GPIOMEM not found; the buttons cannot be read on this system"
    mkdir -p "$PREFIX"
    install -m 755 "$HERE/adsb_display.py" "$PREFIX/adsb_display.py"
    install -m 755 "$HERE/adsb_buttons.py" "$PREFIX/adsb_buttons.py"
    cat > "$UNIT" <<EOF
[Unit]
Description=Argon POD buttons for the ADS-B Zero
After=adsb-display.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 $PREFIX/adsb_buttons.py
RuntimeDirectory=adsb-buttons
RuntimeDirectoryMode=0755
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$UNIT"
    SC daemon-reload
    SC enable adsb-buttons.service >/dev/null 2>&1
    SC restart adsb-buttons.service
    # the screen program was updated: restart it so it shows the pages
    SC is-enabled adsb-display.service >/dev/null 2>&1 && SC restart adsb-display.service
    sleep 3
    echo "buttons service: $(SC is-active adsb-buttons.service 2>&1)    screen service: $(SC is-active adsb-display.service 2>&1)"
    echo
    [[ -f "$PREFIX/adsb_biast_setup.sh" ]] && echo "Button 2 (bias tee):  ready" || echo "Button 2 (bias tee):  not available yet - run:  sudo bash adsb_biast_setup.sh install"
    [[ -f "$PREFIX/adsb_sdexport.py" ]]    && echo "Button 4 / 3 (SD copy): ready" || echo "Button 4 (SD copy):   not available yet - run:  sudo bash adsb_sdexport_setup.sh install"
    echo
    echo "Check the wiring first:  sudo bash $0 test"
    ;;
  test)
    [[ -f "$PREFIX/adsb_buttons.py" ]] || die "run install first"
    SC stop adsb-buttons.service 2>/dev/null
    GPIOMEM_ARG=(); [[ "$GPIOMEM" != /dev/gpiomem ]] && GPIOMEM_ARG=(--gpiomem "$GPIOMEM")
    python3 "$PREFIX/adsb_buttons.py" --test 20 "${GPIOMEM_ARG[@]}" 2>&1 | tee "$UHOME/buttons_test.txt"
    SC start adsb-buttons.service 2>/dev/null
    echo; echo "Saved to $UHOME/buttons_test.txt. The button service was started again."
    ;;
  status)
    {
      echo "buttons service : $(SC is-active adsb-buttons.service 2>&1)"
      echo "screen service  : $(SC is-active adsb-display.service 2>&1)"
      echo "gpiomem         : $([[ -e "$GPIOMEM" ]] && echo present || echo MISSING)"
      echo "bias tee script : $([[ -f "$PREFIX/adsb_biast_setup.sh" ]] && echo installed || echo "not installed")"
      echo "sd export       : $([[ -f "$PREFIX/adsb_sdexport.py" ]] && echo installed || echo "not installed")"
      echo "screen state file:"; cat /run/adsb-buttons/ui.json 2>&1; echo
      echo "last service log lines:"; journalctl -u adsb-buttons.service --no-pager -n 15 2>&1
    } | tee "$UHOME/buttons_status.txt"
    echo; echo "Saved to $UHOME/buttons_status.txt"
    ;;
  undo)
    SC disable --now adsb-buttons.service >/dev/null 2>&1
    rm -f "$UNIT"; SC daemon-reload
    echo "Button service removed. The screen keeps showing the stats (page 1 only)."
    ;;
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
