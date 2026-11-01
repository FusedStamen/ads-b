#!/usr/bin/env bash
# pull_zero.sh - run on the uConsole (or any machine on the same network).
# Pulls the Zero's logs over WiFi with rsync, then writes a merged report/file.
# Nothing is written into the game's loot folder unless you pass --stage.
#
#   ./pull_zero.sh                      # pull + merge report against uConsole sessions
#   ./pull_zero.sh ~/python/WatchDogsGo/loot/2026-10-1*   # choose which sessions
#   ./pull_zero.sh --stage              # pull, then create ONE new loot session folder
#                                       # holding only Zero ICAOs not staged before
#                                       # (the game's uploader picks it up as pending)
#
# Environment overrides:
#   ZERO_USER (default: current user)   ZERO_HOST (default: adsbzero.local)
#   DEST (default: ~/adsb-zero-pull)    ZERO_SRC (testing: any local dir with a trailing /)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZERO_USER="${ZERO_USER:-${USER:-$(id -un)}}"
ZERO_HOST="${ZERO_HOST:-adsbzero.local}"
DEST="${DEST:-$HOME/adsb-zero-pull}"
SRC="${ZERO_SRC:-$ZERO_USER@$ZERO_HOST:/var/lib/adsb-zero/}"
LOOT="${LOOT:-$HOME/python/WatchDogsGo/loot}"

STAGE=0
if [[ "${1:-}" == "--stage" ]]; then STAGE=1; shift; fi

mkdir -p "$DEST"
echo "pulling $SRC -> $DEST"
rsync -a --partial --timeout=30 "$SRC" "$DEST/"

ZERO_CSV="$DEST/adsb_aircraft.csv"
if [[ ! -f "$ZERO_CSV" ]]; then
  echo "no adsb_aircraft.csv on the Zero yet (nothing logged?)" >&2
  exit 1
fi
echo "Zero: $(($(wc -l < "$ZERO_CSV") - 1)) unique ICAOs logged"

if [[ $STAGE -eq 1 ]]; then
  python3 "$HERE/merge_adsb.py" "$ZERO_CSV" --out-dir "$LOOT" --skip-file "$DEST/staged_icaos.txt"
  exit 0
fi

# uConsole sessions to compare against (default: every session folder with an ADS-B file)
if [[ $# -gt 0 ]]; then
  UC=("$@")
else
  mapfile -t UC < <(ls -d "$LOOT"/20*/ 2>/dev/null || true)
fi

if [[ ${#UC[@]} -eq 0 ]]; then
  echo "no uConsole sessions found under $LOOT; Zero data is in $ZERO_CSV"
  exit 0
fi

python3 "$HERE/merge_adsb.py" "$ZERO_CSV" "${UC[@]}" --out-file "$DEST/merged_adsb_aircraft.csv"
