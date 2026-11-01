#!/usr/bin/env python3
"""
validate_for_game.py - check an adsb_aircraft.csv against what WatchDogsGo's uploader accepts.

It applies the SAME row rules as the game's _parse_adsb (plugins/wardrive_upload.py), row by
row, and tells you how many aircraft would be offered for upload, why any rows are skipped, and
whether anything looks risky for the server (odd ICAO format, impossible timestamps, case
variants of the same ICAO).

Usage (no sudo needed):
  python3 validate_for_game.py                      # checks /var/lib/adsb-zero/adsb_aircraft.csv
  python3 validate_for_game.py FILE_OR_FOLDER ...   # any CSV, or a loot session folder
Output: printed here and saved to ~/game_validation.txt

Limits, stated plainly: this checks the rules visible in the game's code. It cannot see the
server's own checks. The real confirmation is the upload result (aircraft_imported).
"""
import csv
import os
import re
import sys
import time

HEADER = ["timestamp", "icao", "callsign", "lat", "lon",
          "alt_ft", "speed_kt", "heading", "squawk"]
DEFAULT = "/var/lib/adsb-zero/adsb_aircraft.csv"
MIN_TS = 1704067200  # 2024-01-01 UTC: anything earlier means the clock was wrong when logged
ICAO_RE = re.compile(r"^[0-9A-F]{6}$")


def classify(parts):
    """Exactly the game's per-row acceptance rules. Returns a reason string or 'ok'."""
    if len(parts) < 8:
        return "short_row"
    try:
        lat = float(parts[3]) if parts[3] else 0.0
        lon = float(parts[4]) if parts[4] else 0.0
    except ValueError:
        return "non_numeric_position"
    if lat == 0.0 and lon == 0.0:
        return "zero_or_blank_position"
    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
        return "position_out_of_range"
    if not parts[1]:
        return "empty_icao"
    return "ok"


def find_csvs(path):
    if os.path.isdir(path):
        found = []
        for root, _d, files in os.walk(path):
            if "adsb_aircraft.csv" in files:
                found.append(os.path.join(root, "adsb_aircraft.csv"))
        return sorted(found)
    return [path]


def analyze(paths, now=None):
    now = now or time.time()
    r = {"files": len(paths), "rows": 0, "header_ok": True, "skipped": {},
         "uploadable": {}, "dup_rows": 0, "bad_icao": [], "lower_icao": 0,
         "bad_ts": 0, "ts_min": None, "ts_max": None, "bad_alt": 0,
         "lat": [None, None], "lon": [None, None], "case_variants": 0}
    seen_upper = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            if header != HEADER:
                r["header_ok"] = False
            for parts in rd:
                r["rows"] += 1
                why = classify(parts)
                if why != "ok":
                    r["skipped"][why] = r["skipped"].get(why, 0) + 1
                    continue
                icao = parts[1]
                if icao in r["uploadable"]:
                    r["dup_rows"] += 1
                    continue
                try:
                    alt = int(float(parts[5])) if parts[5] else 0
                    spd = int(float(parts[6])) if parts[6] else 0
                    hdg = int(float(parts[7])) if parts[7] else 0
                except ValueError:
                    alt = spd = hdg = 0
                r["uploadable"][icao] = {
                    "icao": icao, "callsign": parts[2], "lat": float(parts[3]),
                    "lon": float(parts[4]), "alt_ft": alt, "speed_kt": spd,
                    "heading": hdg, "first_seen": parts[0], "type": "ADSB"}
                if not ICAO_RE.match(icao):
                    if ICAO_RE.match(icao.upper()):
                        r["lower_icao"] += 1
                    elif len(r["bad_icao"]) < 5:
                        r["bad_icao"].append(icao)
                seen_upper.setdefault(icao.upper(), set()).add(icao)
                try:
                    ts = float(parts[0])
                    if ts < MIN_TS or ts > now + 86400:
                        r["bad_ts"] += 1
                    r["ts_min"] = ts if r["ts_min"] is None else min(r["ts_min"], ts)
                    r["ts_max"] = ts if r["ts_max"] is None else max(r["ts_max"], ts)
                except ValueError:
                    r["bad_ts"] += 1
                if alt < -2000 or alt > 60000:
                    r["bad_alt"] += 1
                la, lo = float(parts[3]), float(parts[4])
                for k, v in (("lat", la), ("lon", lo)):
                    cur = r[k]
                    cur[0] = v if cur[0] is None else min(cur[0], v)
                    cur[1] = v if cur[1] is None else max(cur[1], v)
    r["case_variants"] = sum(1 for v in seen_upper.values() if len(v) > 1)
    return r


def fmt_ts(ts):
    return "n/a" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def report(r):
    out = []
    n = len(r["uploadable"])
    out.append(f"Files checked:                {r['files']}")
    out.append(f"Header matches the game:      {'yes' if r['header_ok'] else 'NO (game ignores the header row, but check the file)'}")
    out.append(f"Data rows read:               {r['rows']}")
    out.append(f"AIRCRAFT THE GAME WOULD UPLOAD: {n}")
    if r["skipped"]:
        out.append("Rows the game skips:")
        for k, v in sorted(r["skipped"].items()):
            out.append(f"    {k}: {v}")
    if r["dup_rows"]:
        out.append(f"Repeat rows for an ICAO already counted: {r['dup_rows']} (the game keeps the first)")
    if n:
        out.append(f"First seen:  {fmt_ts(r['ts_min'])}")
        out.append(f"Last first_seen: {fmt_ts(r['ts_max'])}")
        out.append(f"Position box: lat {r['lat'][0]:.2f}..{r['lat'][1]:.2f}, lon {r['lon'][0]:.2f}..{r['lon'][1]:.2f}")
    warns = []
    if not r["header_ok"]:
        warns.append("header differs from the game's 9 columns")
    if r["bad_icao"]:
        warns.append(f"{len(r['bad_icao'])}+ ICAOs are not 6 hex characters, e.g. {r['bad_icao'][:3]}")
    if r["lower_icao"]:
        warns.append(f"{r['lower_icao']} ICAOs are lower case (the game's own files use upper case; the server may count them as different aircraft)")
    if r["case_variants"]:
        warns.append(f"{r['case_variants']} ICAOs appear in more than one letter case")
    if r["bad_ts"]:
        warns.append(f"{r['bad_ts']} rows have timestamps before 2024 or in the future (clock was wrong when logged)")
    if r["bad_alt"]:
        warns.append(f"{r['bad_alt']} rows have implausible altitudes")
    if n == 0:
        warns.append("nothing would be uploaded")
    out.append("")
    if warns:
        out.append("WARNINGS:")
        out += [f"  - {w}" for w in warns]
    else:
        out.append("VERDICT: OK. Every uploadable row passes the game's rules and looks sane.")
    if n:
        out.append("")
        out.append("Example of exactly what the game would send for the first aircraft:")
        out.append("  " + str(next(iter(r["uploadable"].values()))))
    out.append("")
    out.append("Reminder: this checks the game's visible rules only. The real confirmation is the")
    out.append("aircraft_imported count the game shows after the first upload.")
    return "\n".join(out)


def main(argv):
    targets = argv[1:] or [DEFAULT]
    paths = []
    for t in targets:
        found = find_csvs(os.path.expanduser(t))
        if not found or not all(os.path.isfile(p) for p in found):
            print(f"cannot find {t}", file=sys.stderr)
            return 1
        paths += found
    text = report(analyze(paths))
    print(text)
    home = os.path.expanduser("~")
    try:
        with open(os.path.join(home, "game_validation.txt"), "w") as f:
            f.write(text + "\n")
        print(f"\nSaved to {os.path.join(home, 'game_validation.txt')}")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
