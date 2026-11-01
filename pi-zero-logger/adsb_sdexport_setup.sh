#!/usr/bin/env bash
# adsb_sdexport_setup.sh - copy the ADS-B logs onto the SD card's FAT boot partition so you can
# pull the card, plug it into a Windows PC, and read the files with no extra software.
#
#   sudo bash adsb_sdexport_setup.sh install   install + start the exporter (copies every 5 minutes)
#   sudo bash adsb_sdexport_setup.sh status    show what is exported and when (writes ~/sdexport_status.txt)
#   sudo bash adsb_sdexport_setup.sh now       copy right now
#   sudo bash adsb_sdexport_setup.sh undo      remove the exporter (exported files are left alone)
#
# On the Windows PC the files appear on the small drive named "bootfs" in the folder  adsb-export\
#   adsb-export\adsb_aircraft.csv      the game-format file (this is the one to upload)
#   adsb-export\summary\               daily detail files
#   adsb-export\status.txt             when it was exported, counts, clock state
# Env overrides for tests: SRC_DIR DEST_DIR PREFIX UNIT_DIR NO_SYSTEMCTL
set -u
MODE="${1:-}"
PREFIX="${PREFIX:-/opt/adsb-zero}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SRC_DIR="${SRC_DIR:-/var/lib/adsb-zero}"
DEST_DIR="${DEST_DIR:-/boot/firmware/adsb-export}"
SCRIPT="$PREFIX/adsb_sdexport.py"
UNIT="$UNIT_DIR/adsb-sdexport.service"
SC() { [[ -n "${NO_SYSTEMCTL:-}" ]] || systemctl "$@"; }
die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo:  sudo bash $0 ${MODE:-install}"

write_py() {
cat > "$SCRIPT" <<'EOF'
#!/usr/bin/env python3
"""Copy adsb logs to the boot partition. Atomic (temp file then rename), only when content changed,
final copy on shutdown (SIGTERM)."""
import argparse, hashlib, json, os, shutil, signal, sys, time

def digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()

def copy_if_changed(src, dst):
    if os.path.exists(dst) and os.path.getsize(src) == os.path.getsize(dst) and digest(src) == digest(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return True

def export(src_dir, dest_dir, status_src):
    changed = 0
    if not os.path.isdir(src_dir):
        return -1
    os.makedirs(dest_dir, exist_ok=True)
    for root, _d, files in os.walk(src_dir):
        for fn in files:
            if fn.endswith(".tmp"):
                continue
            s = os.path.join(root, fn)
            rel = os.path.relpath(s, src_dir)
            changed += copy_if_changed(s, os.path.join(dest_dir, rel))
    st = {}
    try:
        st = json.load(open(status_src))
    except Exception:
        pass
    lines = ["Exported (Pi clock): " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
             "Files changed this pass: %d" % changed]
    for k in ("all_time_unique", "today_unique", "clock", "last_new_icao", "updated"):
        if k in st:
            lines.append("%s: %s" % (k, st[k]))
    lines.append("Upload adsb_aircraft.csv. Rows tagged unsynced/corrected mean the Pi had no internet clock.")
    tmp = os.path.join(dest_dir, "status.txt.tmp")
    open(tmp, "w").write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(dest_dir, "status.txt"))
    try:
        os.sync()
    except Exception:
        pass
    return changed

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/var/lib/adsb-zero")
    ap.add_argument("--dest", default="/boot/firmware/adsb-export")
    ap.add_argument("--status", default="/run/adsb-zero/status.json")
    ap.add_argument("--interval", type=float, default=300)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if a.once:
        n = export(a.src, a.dest, a.status)
        print("exported, files changed: %d" % n if n >= 0 else "source folder missing")
        return 0 if n >= 0 else 1
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    while not stop:
        try:
            export(a.src, a.dest, a.status)
        except Exception as e:
            print("export error:", e, file=sys.stderr)
        t = time.time()
        while not stop and time.time() - t < a.interval:
            time.sleep(1)
    try:
        export(a.src, a.dest, a.status)   # final copy at shutdown
    except Exception as e:
        print("final export error:", e, file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 755 "$SCRIPT"
}

case "$MODE" in
  install)
    [[ -d "$SRC_DIR" ]] || die "$SRC_DIR not found: install the logger first"
    mkdir -p "$PREFIX" "$(dirname "$DEST_DIR")" || die "cannot create folders"
    write_py
    python3 -c "import ast;ast.parse(open('$SCRIPT').read())" || die "exporter does not parse"
    cat > "$UNIT" <<EOF
[Unit]
Description=Copy ADS-B logs to the boot partition (readable on Windows)
After=adsb-unique-logger.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $SCRIPT --src $SRC_DIR --dest $DEST_DIR
Restart=always
RestartSec=10
TimeoutStopSec=30
Nice=10

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$UNIT"
    SC daemon-reload
    SC enable adsb-sdexport.service >/dev/null 2>&1
    SC restart adsb-sdexport.service
    sleep 3
    python3 "$SCRIPT" --src "$SRC_DIR" --dest "$DEST_DIR" --once
    echo
    echo "Exporter installed. Files are copied to $DEST_DIR every 5 minutes and once more at shutdown."
    echo "To pull the card safely: run  sudo shutdown now  , wait for the green light to stop blinking, then unplug power."
    echo "On Windows, open the small drive (bootfs) -> adsb-export -> adsb_aircraft.csv"
    ;;
  now)
    [[ -f "$SCRIPT" ]] || die "run install first"
    python3 "$SCRIPT" --src "$SRC_DIR" --dest "$DEST_DIR" --once
    ;;
  status)
    OUT="${HOME_OVERRIDE:-$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)}/sdexport_status.txt"
    {
      echo "== service"; SC is-active adsb-sdexport.service 2>&1
      echo "== exported files"; ls -la --time-style=long-iso "$DEST_DIR" 2>&1
      echo "== status.txt"; cat "$DEST_DIR/status.txt" 2>&1
      echo "== rows exported / logged"; wc -l "$DEST_DIR/adsb_aircraft.csv" "$SRC_DIR/adsb_aircraft.csv" 2>&1
      echo "== boot partition"; df -h "$(dirname "$DEST_DIR")" 2>&1; mount | grep -E "firmware" 2>&1
    } | tee "$OUT"
    echo; echo "Saved to $OUT"
    ;;
  undo)
    SC disable --now adsb-sdexport.service >/dev/null 2>&1
    rm -f "$UNIT" "$SCRIPT"
    SC daemon-reload
    echo "Exporter removed. Files already in $DEST_DIR were left in place."
    ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
