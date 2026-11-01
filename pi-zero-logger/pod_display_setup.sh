#!/usr/bin/env bash
# pod_display_setup.sh - set up (or undo) the Argon POD 2.8" screen on Raspberry Pi OS Trixie,
# WITHOUT running Argon's installer.
#
#   sudo bash pod_display_setup.sh install      install and configure; then reboot
#   sudo bash pod_display_setup.sh status       after the reboot: test the screen, save a report
#   sudo bash pod_display_setup.sh rotate 90    set screen rotation (0, 90, 180 or 270), then reboot
#   sudo bash pod_display_setup.sh undo         remove everything this script added; then reboot
#
# Optional for install:  FONT=VGA8x8 sudo -E bash pod_display_setup.sh install
#                        (smaller console font: 40x30 characters instead of 40x15)
#
# Where things go:
#   - Backups of config.txt and cmdline.txt:  ~/pod_display_backup/
#   - status report:                          ~/pod_display_status.txt
#   - To ask for help, attach that file to your issue.
#
# What the screen is (decoded from Argon's overlay, sha256 69221fb7...a26b):
#   ILI9340-class SPI display on SPI0 CS0, data/command = GPIO22, reset = GPIO27, 16 MHz,
#   ADS7846 resistive touch on CS1 with pen-down interrupt on GPIO17. No backlight pin is used.
#   The four buttons (GPIO 16/20/21/26) and the IR receiver (GPIO23) are not touched here.
# This gives a text console on the screen. It is not a desktop.

set -u
MODE="${1:-}"
ARG2="${2:-}"
BOOT="${BOOT:-/boot/firmware}"
OVERLAY_URL="${OVERLAY_URL:-https://download.argon40.com/pod/tft9341-overlay.dtb}"
EXPECT_SHA="${EXPECT_SHA:-69221fb7e5cf9a2b3a63c3f7e75276864b60ccf9b2e6ef763eb8ce92a3e4a26b}"
ROTATE="${ROTATE:-270}"
FONT="${FONT:-}"
USER_NAME="${SUDO_USER:-$(id -un)}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
BACKUP_DIR="${BACKUP_DIR:-$HOME_DIR/pod_display_backup}"
STATUS_FILE="${STATUS_FILE:-$HOME_DIR/pod_display_status.txt}"
CFG="$BOOT/config.txt"
CMD="$BOOT/cmdline.txt"
DTBO="$BOOT/overlays/tft9341.dtbo"
START="# >>> pod-display (managed by pod_display_setup.sh) >>>"
END="# <<< pod-display <<<"
MARK_OVERLAY="$BACKUP_DIR/.installed_overlay"
MARK_FONT="$BACKUP_DIR/.added_font"

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,14p' "$0"; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo:  sudo bash $0 ${MODE:-install}"
[[ -f "$CFG" ]] || die "cannot find $CFG (is this Raspberry Pi OS?)"
[[ -f "$CMD" ]] || die "cannot find $CMD"

valid_rotate() { [[ "$1" == "0" || "$1" == "90" || "$1" == "180" || "$1" == "270" ]]; }
has_block() { grep -qF "$START" "$CFG"; }
remove_block() { sed -i "\|^$START|,\|^$END|d" "$CFG"; }

do_install() {
  valid_rotate "$ROTATE" || die "ROTATE must be 0, 90, 180 or 270"
  # A tft9341 overlay line outside our block means Argon's own installer (or something else) ran
  if sed "\|^$START|,\|^$END|d" "$CFG" | grep -q -E '^\s*dtoverlay=tft9341'; then
    die "config.txt already has a dtoverlay=tft9341 line that this script did not add. Run Argon's argonpod-uninstall first, or open an issue."
  fi
  if [[ "${SKIP_DRIVER_CHECK:-0}" != "1" ]]; then
    modinfo -F filename fb_ili9340 >/dev/null 2>&1 || die "this kernel has no fb_ili9340 driver, so the screen cannot be driven this way. Nothing was changed. Please open an issue."
  fi

  TMP="$(mktemp)"
  trap 'rm -f "$TMP"' EXIT
  echo "Downloading overlay..."
  curl -fsSL --max-time 30 "$OVERLAY_URL" -o "$TMP" || die "download failed (internet?). Nothing was changed."
  GOT="$(sha256sum "$TMP" | cut -d' ' -f1)"
  if [[ "$GOT" != "$EXPECT_SHA" ]]; then
    die "the overlay file differs from the one this script was written against (sha256 $GOT). Nothing was changed. Please open an issue with this message."
  fi

  mkdir -p "$BACKUP_DIR"
  TS="$(date +%Y%m%d-%H%M%S)"
  [[ -f "$BACKUP_DIR/config.txt.orig" ]]  || cp -p "$CFG" "$BACKUP_DIR/config.txt.orig"
  [[ -f "$BACKUP_DIR/cmdline.txt.orig" ]] || cp -p "$CMD" "$BACKUP_DIR/cmdline.txt.orig"
  cp -p "$CFG" "$BACKUP_DIR/config.txt.$TS"
  cp -p "$CMD" "$BACKUP_DIR/cmdline.txt.$TS"

  mkdir -p "$(dirname "$DTBO")"
  install -m 755 "$TMP" "$DTBO"
  touch "$MARK_OVERLAY"

  remove_block
  # ensure the file ends with a newline before appending
  [[ -n "$(tail -c1 "$CFG")" ]] && echo >> "$CFG"
  {
    echo "$START"
    echo "[all]"
    echo "dtparam=spi=on"
    echo "dtoverlay=tft9341,rotate=$ROTATE"
    echo "$END"
  } >> "$CFG"

  if [[ -n "$FONT" ]] && ! grep -q 'fbcon=font:' "$CMD"; then
    sed -i "1s/[[:space:]]*\$/ fbcon=font:$FONT/" "$CMD"
    touch "$MARK_FONT"
  fi

  chown -R "$USER_NAME":"$(id -gn "$USER_NAME" 2>/dev/null || echo "$USER_NAME")" "$BACKUP_DIR" 2>/dev/null || true
  echo
  echo "Installed. Backups are in $BACKUP_DIR"
  echo "Added to $CFG:"
  sed -n "\|^$START|,\|^$END|p" "$CFG" | sed 's/^/    /'
  echo
  echo "Reboot now:   sudo reboot"
  echo "After it is back (the screen should show boot text or a login prompt):"
  echo "              sudo bash $0 status"
}

do_undo() {
  if has_block; then remove_block; echo "Removed the pod-display block from $CFG"; else echo "No pod-display block found in $CFG"; fi
  if [[ -f "$MARK_OVERLAY" && -f "$DTBO" ]]; then rm -f "$DTBO"; rm -f "$MARK_OVERLAY"; echo "Removed $DTBO"; fi
  if [[ -f "$MARK_FONT" ]]; then
    sed -i 's/ *fbcon=font:[^ ]*//' "$CMD"; rm -f "$MARK_FONT"; echo "Removed the fbcon font setting from $CMD"
  fi
  echo "Original copies of your boot files are still in $BACKUP_DIR"
  echo "Reboot to finish:   sudo reboot"
}

do_rotate() {
  valid_rotate "$ARG2" || die "usage: sudo bash $0 rotate 0|90|180|270"
  has_block || die "no pod-display block in $CFG; run install first"
  sed -i "s/^dtoverlay=tft9341,rotate=.*/dtoverlay=tft9341,rotate=$ARG2/" "$CFG"
  echo "Rotation set to $ARG2. Reboot to apply:   sudo reboot"
}

do_status() {
  {
    echo "===== pod display status ====="
    date
    echo
    echo "--- config block ---"
    if has_block; then sed -n "\|^$START|,\|^$END|p" "$CFG"; else echo "(no pod-display block in $CFG)"; fi
    echo "--- overlay file ---"
    ls -l "$DTBO" 2>&1
    echo "--- cmdline ---"
    cat "$CMD"
    echo "--- framebuffers ---"
    ls -l /dev/fb* 2>&1
    FBTFT=""
    for f in /sys/class/graphics/fb*; do
      [[ -d "$f" ]] || continue
      n="$(cat "$f/name" 2>/dev/null)"
      echo "$(basename "$f"): name=$n size=$(cat "$f/virtual_size" 2>/dev/null) bpp=$(cat "$f/bits_per_pixel" 2>/dev/null)"
      case "$n" in *ili9340*|*ili9341*|*fbtft*) FBTFT="$(basename "$f")";; esac
    done
    echo "--- loaded drivers ---"
    lsmod | grep -i -E 'fbtft|fb_ili|ads7846|spi_bcm' || echo "(none of fbtft/ads7846 loaded)"
    echo "--- touch input ---"
    grep -i -A3 -B1 'ads7846' /proc/bus/input/devices 2>/dev/null || echo "(no ADS7846 input device)"
    echo "--- kernel messages ---"
    dmesg | grep -i -E 'fbtft|ili9|ads7846|spi0|tft9341|fb[0-9]' | tail -25
    echo "--- power ---"
    vcgencmd get_throttled 2>&1
    echo
    if [[ -n "$FBTFT" ]]; then
      echo "Screen framebuffer found: /dev/$FBTFT"
    else
      echo "RESULT: no SPI display framebuffer found. The overlay did not bind to the driver."
    fi
  } 2>&1 | tee "$STATUS_FILE"

  # visible test on the screen itself
  FBDEV="$(for f in /sys/class/graphics/fb*; do n="$(cat "$f/name" 2>/dev/null)"; case "$n" in *ili9340*|*ili9341*|*fbtft*) basename "$f";; esac; done | head -1)"
  if [[ -n "$FBDEV" ]]; then
    W_H="$(cat /sys/class/graphics/$FBDEV/virtual_size)"; W="${W_H%,*}"; H="${W_H#*,}"
    BPP="$(cat /sys/class/graphics/$FBDEV/bits_per_pixel)"
    BYTES=$(( W * H * BPP / 8 ))
    echo
    echo "Test 1: random colour noise for 3 seconds on /dev/$FBDEV (should fill the whole screen)..."
    head -c "$BYTES" /dev/urandom > "/dev/$FBDEV" 2>/dev/null
    sleep 3
    printf '\033[2J\033[H POD DISPLAY OK\n\n %s\n' "$(hostname)" > /dev/tty1 2>/dev/null
    echo "Test 2: wrote 'POD DISPLAY OK' to the console (tty1)."
    echo "  - noise but no text afterwards: the screen works, the console is on another framebuffer.."
    echo "  - nothing at all: backlight or wiring problem.."
    echo "  - sideways or upside down: sudo bash $0 rotate 90   (or 0, 180, 270), then reboot."
  fi
  chown "$USER_NAME":"$(id -gn "$USER_NAME" 2>/dev/null || echo "$USER_NAME")" "$STATUS_FILE" 2>/dev/null || true
  echo
  echo "Report saved: $STATUS_FILE  (attach it when asking for help)"
}

case "$MODE" in
  install) do_install ;;
  undo)    do_undo ;;
  rotate)  do_rotate ;;
  status)  do_status ;;
  *)       usage ;;
esac
