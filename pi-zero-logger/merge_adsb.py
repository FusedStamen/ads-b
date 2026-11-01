#!/usr/bin/env python3
"""
merge_adsb.py - merge several WatchDogsGo-format adsb_aircraft.csv files into
one new file with a single row per ICAO (the earliest valid position).

Inputs can be CSV files or directories (every adsb_aircraft.csv under them is
used). Each INPUT argument is treated as one "source" for the report, so pass
the Zero's file and the uConsole's session folders as separate arguments to see
who contributed what:

  merge_adsb.py zero/adsb_aircraft.csv ~/python/WatchDogsGo/loot/2026-10-1* \
        --out-file merged.csv

With --out-dir the result goes to <out-dir>/<YYYY-MM-DD_HH-MM-SS>/adsb_aircraft.csv
(the game's own session-folder naming; an existing folder name is never reused).
Use --out-file to write one file instead.

To hand the Zero's aircraft to the game, stage only what is new:

  merge_adsb.py zero/adsb_aircraft.csv --out-dir ~/python/WatchDogsGo/loot \
        --skip-file staged_icaos.txt

The game's uploader treats any loot folder holding an adsb_aircraft.csv as a pending
session, so that folder is offered for upload next time. --skip-file remembers which
ICAOs were staged, so each run creates a folder only for ICAOs not staged before
(and none at all when there is nothing new).

Row validity follows the game's uploader exactly: at least 8 columns, numeric
lat/lon in range, not both zero. ICAOs are normalised to upper case.
Streams the input, so the 1+ GB uConsole files are fine.
"""
import argparse
import csv
import glob
import os
import sys
import tempfile
import time

HEADER = ["timestamp", "icao", "callsign", "lat", "lon",
          "alt_ft", "speed_kt", "heading", "squawk"]


def find_csvs(path):
    if os.path.isdir(path):
        out = []
        for root, _dirs, files in os.walk(path):
            if "adsb_aircraft.csv" in files:
                out.append(os.path.join(root, "adsb_aircraft.csv"))
        return sorted(out)
    return [path] if os.path.isfile(path) else []


def valid_row(parts):
    """Same acceptance rules as the game's _parse_adsb."""
    if len(parts) < 8:
        return False
    try:
        lat = float(parts[3]) if parts[3] else 0.0
        lon = float(parts[4]) if parts[4] else 0.0
    except ValueError:
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
        return False
    return bool(parts[1])


def ts_key(s):
    try:
        return float(s)
    except ValueError:
        return float("inf")


def merge(sources):
    """sources: list of (label, [csv paths]). Returns (best, per_source_sets, stats)."""
    best = {}          # icao -> row (list of str), earliest timestamp wins
    per_source = []    # list of sets of ICAOs
    rows_read = []
    for label, paths in sources:
        seen = set()
        n = 0
        for path in paths:
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                rd = csv.reader(f)
                next(rd, None)
                for parts in rd:
                    n += 1
                    if not valid_row(parts):
                        continue
                    icao = parts[1].strip().upper()
                    seen.add(icao)
                    cur = best.get(icao)
                    if cur is None or ts_key(parts[0]) < ts_key(cur[0]):
                        row = list(parts[:9]) + [""] * max(0, 9 - len(parts))
                        row[1] = icao
                        best[icao] = row
        per_source.append(seen)
        rows_read.append(n)
    return best, per_source, rows_read


def write_out(best, path):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for row in sorted(best.values(), key=lambda r: ts_key(r[0])):
            w.writerow(row)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="CSV files, folders, or globs")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out-dir", help="create <out-dir>/<session-name>/adsb_aircraft.csv")
    g.add_argument("--out-file", help="write this exact file")
    ap.add_argument("--session-name", default=None,
                    help="folder name under --out-dir (default: now, YYYY-MM-DD_HH-MM-SS)")
    ap.add_argument("--skip-file", default=None,
                    help="text file of ICAOs already staged. Those are left out of the "
                         "output, and the ICAOs written are appended to it afterwards. "
                         "If nothing is new, no output is created.")
    a = ap.parse_args(argv)

    sources = []
    for arg in a.inputs:
        paths = []
        for m in (sorted(glob.glob(os.path.expanduser(arg))) or [arg]):
            paths += find_csvs(m)
        if not paths:
            print(f"warning: nothing found for {arg}", file=sys.stderr)
        sources.append((arg, paths))
    if not any(p for _l, p in sources):
        print("no input files found", file=sys.stderr)
        return 1

    best, per_source, rows_read = merge(sources)

    skipped = 0
    if a.skip_file:
        staged = set()
        if os.path.isfile(a.skip_file):
            with open(a.skip_file, encoding="utf-8") as f:
                staged = {ln.strip().upper() for ln in f if ln.strip()}
        skipped = sum(1 for k in best if k in staged)
        best = {k: v for k, v in best.items() if k not in staged}
        if not best:
            print(f"no new ICAOs ({skipped} already staged); nothing written")
            return 0

    if a.out_file:
        out = a.out_file
    else:
        if a.session_name:
            name = a.session_name
        else:
            t = time.time()
            name = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(t))
            while os.path.exists(os.path.join(a.out_dir, name)):
                t += 1  # never reuse a session folder name
                name = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(t))
        out = os.path.join(a.out_dir, name, "adsb_aircraft.csv")
    write_out(best, out)

    if a.skip_file:
        # after the output is safely on disk; a crash before this only means a
        # harmless re-stage next time (the server de-duplicates)
        os.makedirs(os.path.dirname(os.path.abspath(a.skip_file)), exist_ok=True)
        with open(a.skip_file, "a", encoding="utf-8") as f:
            f.write("".join(k + "\n" for k in sorted(best)))
            f.flush()
            os.fsync(f.fileno())

    print(f"wrote {len(best)} unique ICAOs -> {out}"
          + (f" ({skipped} already staged, skipped)" if a.skip_file else ""))
    for i, ((label, paths), seen, n) in enumerate(zip(sources, per_source, rows_read)):
        others = set().union(*[s for j, s in enumerate(per_source) if j != i]) \
            if len(per_source) > 1 else set()
        print(f"  [{i}] {label}: {len(paths)} file(s), {n} rows, "
              f"{len(seen)} unique valid ICAOs, {len(seen - others)} seen only here")
    return 0


if __name__ == "__main__":
    sys.exit(main())
