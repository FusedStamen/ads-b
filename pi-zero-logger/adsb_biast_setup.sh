#!/usr/bin/env bash
# adsb_biast_setup.sh - switch the RTL-SDR Blog V4 bias tee (power down the antenna cable, e.g. for
# a preamp) on or off from the Pi. It is OFF by default and stays off until you turn it on.
#
#   sudo bash adsb_biast_setup.sh install   one-time: builds the small bias-tee tool (needs internet,
#                                           a few minutes on a Zero) and hooks it into dump1090-fa
#   sudo bash adsb_biast_setup.sh on        bias tee ON  (restarts the decoder for a few seconds)
#   sudo bash adsb_biast_setup.sh off       bias tee OFF (restarts the decoder for a few seconds)
#   sudo bash adsb_biast_setup.sh status    what is requested, what is installed -> ~/biast_status.txt
#   sudo bash adsb_biast_setup.sh undo      turn it off and remove the hook (leaves everything else alone)
#
# SAFETY: the bias tee puts about 4.5 V on the antenna connector. Turn it on only when a bias-tee
# powered preamp is connected between the antenna and the dongle. A DC-shorted antenna can be damaged
# by it. The software cannot read the actual state back from the dongle: the only proof it is on is
# about 4.5 V between the dongle's SMA center pin and shell (antenna disconnected, multimeter).
# The decoder turns it OFF every time it stops (including shutdown) and re-applies your setting on start.
# Overrides for tests: PREFIX UNIT_DIR STATE RTL_BIAST_BIN SKIP_BUILD NO_SYSTEMCTL HOME_OVERRIDE
set -u
MODE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/adsb-zero}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
STATE="${STATE:-/etc/adsb-zero/biast}"
DROPIN_DIR="$UNIT_DIR/dump1090-fa.service.d"
DROPIN="$DROPIN_DIR/adsb-biast.conf"
SRC_DIR="$PREFIX/biast/src"; BUILD_DIR="$PREFIX/biast/build"
RTL_BIAST_BIN="${RTL_BIAST_BIN:-$BUILD_DIR/src/rtl_biast}"
LIB_DIR="$(dirname "$RTL_BIAST_BIN")"
PIN=aed0ea19f3a273370a13c9009b96313c75d54c7b      # rtlsdrblog/rtl-sdr-blog, the commit I built and tested
SC() { [[ -n "${NO_SYSTEMCTL:-}" ]] || systemctl "$@"; }
die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo:  sudo bash $0 ${MODE:-status}"
UHOME="${HOME_OVERRIDE:-$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)}"

want() { local v; v="$(cat "$STATE" 2>/dev/null | tr -d '[:space:]')"; [[ "$v" == "on" ]] && echo 1 || echo 0; }

set_bias() {   # 0|1 ; never fails the caller
  local n="$1"
  [[ -x "$RTL_BIAST_BIN" ]] || { echo "rtl_biast not installed (run install)"; return 0; }
  LD_LIBRARY_PATH="$LIB_DIR" "$RTL_BIAST_BIN" -d 0 -b "$n" >/dev/null 2>&1 \
    && echo "bias tee set to $([[ $n == 1 ]] && echo ON || echo OFF)" \
    || echo "could not talk to the dongle (is the decoder still holding it, or is it unplugged?)"
  return 0
}

case "$MODE" in
  install)
    if [[ -z "${SKIP_BUILD:-}" ]]; then
      echo "Installing build tools (needs internet)..."
      apt-get install -y git cmake build-essential libusb-1.0-0-dev pkg-config >/dev/null || die "apt install failed (no internet?)"
      mkdir -p "$SRC_DIR"
      if [[ ! -d "$SRC_DIR/.git" ]]; then
        git -C "$SRC_DIR" init -q && git -C "$SRC_DIR" remote add origin https://github.com/rtlsdrblog/rtl-sdr-blog.git
      fi
      git -C "$SRC_DIR" fetch -q --depth 1 origin "$PIN" || die "could not download the source from GitHub"
      git -C "$SRC_DIR" checkout -q FETCH_HEAD || die "checkout failed"
      echo "Building rtl_biast (a few minutes on a Zero)..."
      mkdir -p "$BUILD_DIR"
      ( cd "$BUILD_DIR" && cmake "$SRC_DIR" -DINSTALL_UDEV_RULES=OFF -DDETACH_KERNEL_DRIVER=ON >/dev/null && make rtl_biast 2>&1 | tail -2 ) || die "build failed"
      [[ -x "$RTL_BIAST_BIN" ]] || die "build did not produce rtl_biast"
    fi
    mkdir -p "$(dirname "$STATE")" "$PREFIX" "$DROPIN_DIR"
    [[ -f "$STATE" ]] || echo off > "$STATE"
    install -m 755 "$HERE/adsb_biast_setup.sh" "$PREFIX/adsb_biast_setup.sh"
    cat > "$DROPIN" <<EOF
[Service]
ExecStartPre=+/bin/bash $PREFIX/adsb_biast_setup.sh apply
ExecStopPost=+/bin/bash $PREFIX/adsb_biast_setup.sh stopoff
EOF
    chmod 644 "$DROPIN"
    SC daemon-reload
    echo "Installed. Bias tee is OFF. Turn it on with:  sudo bash $0 on"
    ;;
  apply)   set_bias "$(want)"; exit 0 ;;        # run by systemd before the decoder starts
  stopoff) set_bias 0; exit 0 ;;                 # run by systemd after the decoder stops
  on|off)
    [[ -f "$DROPIN" ]] || die "run install first"
    echo "$MODE" > "$STATE"
    if [[ "$MODE" == on ]]; then
      echo "Turning the bias tee ON. Make sure a bias-tee powered preamp is connected (not a DC-shorted antenna)."
    fi
    SC restart dump1090-fa.service
    sleep 4
    echo "Requested: $MODE   decoder: $(SC is-active dump1090-fa.service 2>/dev/null)"
    echo "Verify for real with a multimeter: about 4.5 V at the dongle's SMA center pin = ON, 0 V = OFF."
    ;;
  status)
    {
      echo "requested state : $(want | sed 's/1/ON/;s/0/OFF/') (file $STATE: $(cat "$STATE" 2>/dev/null || echo missing))"
      echo "rtl_biast tool  : $([[ -x "$RTL_BIAST_BIN" ]] && echo present || echo MISSING)"
      echo "hook installed  : $([[ -f "$DROPIN" ]] && echo yes || echo NO)"
      echo "decoder         : $(SC is-active dump1090-fa.service 2>&1)"
      echo "dongle          : $(lsusb 2>/dev/null | grep -i -E '0bda:28(32|38)' | head -1)"
      echo "last bias-tee actions from the decoder service:"
      journalctl -u dump1090-fa.service --no-pager -n 40 2>/dev/null | grep -i -E "bias" | tail -4
      echo "(Software cannot read the real state back; measure ~4.5 V at the SMA center pin to be sure.)"
    } 2>&1 | tee "$UHOME/biast_status.txt"
    echo; echo "Saved to $UHOME/biast_status.txt"
    ;;
  undo)
    echo off > "$STATE" 2>/dev/null
    rm -f "$DROPIN"; rmdir "$DROPIN_DIR" 2>/dev/null
    SC daemon-reload
    SC stop dump1090-fa.service; set_bias 0; SC start dump1090-fa.service
    echo "Hook removed and bias tee turned off. (The built tool in $PREFIX/biast was left; delete it if you like.)"
    ;;
  *) sed -n '2,17p' "$0"; exit 1 ;;
esac
