#!/usr/bin/env python3
"""
adsb_unique_logger.py - compact unique-ICAO logger for dump1090-fa / readsb.

Polls aircraft.json and keeps ONE record per aircraft, not one per message.

Outputs (all under --data-dir, default /var/lib/adsb-zero):
  adsb_aircraft.csv            WatchDogsGo format, one row per ICAO, all-time.
                               Row = first valid position ever seen for that ICAO.
                               Header: timestamp,icao,callsign,lat,lon,alt_ft,
                                       speed_kt,heading,squawk
  summary/summary_YYYY-MM-DD.csv   per UTC day: first/last seen, first position,
                               best (farthest) position, max altitude, max RSSI.
  (status file, tmpfs)         small JSON for a screen / quick check.

SD card friendliness:
  - nothing is written per poll; new ICAOs are batched and appended every
    --flush seconds (default 60) and fsynced once
  - summaries are rewritten atomically only for days that changed, every
    --summary-flush seconds (default 600) and on shutdown
  - old summary files are deleted after --keep-days (default 400)

Clock handling (Pi Zero has no RTC):
  - rows written while the system clock is not NTP-synchronised are tagged
    clock=unsynced in the summary
  - if the process keeps running and the clock later jumps (NTP sync), the
    timestamps of those rows are shifted by the jump, in both the master CSV
    and the summaries, and tagged clock=corrected
  - if the process restarts before a sync, those rows stay unsynced (the
    aircraft still count; only the timestamp is approximate)

Only aircraft WITH a valid position are logged, because the game's uploader
skips rows without one (lat and lon both 0 or blank).
"""
import argparse
import csv
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

GAME_HEADER = ["timestamp", "icao", "callsign", "lat", "lon",
               "alt_ft", "speed_kt", "heading", "squawk"]
SUMMARY_COLS = ["icao", "first_seen", "last_seen", "callsign", "first_lat",
                "first_lon", "best_lat", "best_lon", "best_alt_ft",
                "best_range_nm", "max_alt_ft", "max_rssi", "polls", "clock"]
STEP_THRESHOLD = 5.0  # seconds of wall-vs-monotonic drift counted as a clock step


def log(msg):
    print(msg, flush=True)


def utc_day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def haversine_nm(lat1, lon1, lat2, lon2):
    r = 3440.065  # earth radius in nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class Rec:
    icao: str
    first_seen: int
    last_seen: int
    callsign: str = ""
    first_lat: float = 0.0
    first_lon: float = 0.0
    best_lat: float = 0.0
    best_lon: float = 0.0
    best_alt_ft: int = 0
    best_range_nm: float = -1.0   # -1 = station position unknown
    max_alt_ft: int = 0
    max_rssi: float = -99.9
    polls: int = 0
    clock: str = "ok"             # ok | unsynced | corrected

    def to_row(self):
        return [self.icao, self.first_seen, self.last_seen, self.callsign,
                f"{self.first_lat:.6f}", f"{self.first_lon:.6f}",
                f"{self.best_lat:.6f}", f"{self.best_lon:.6f}",
                self.best_alt_ft, f"{self.best_range_nm:.1f}",
                self.max_alt_ft, f"{self.max_rssi:.1f}", self.polls, self.clock]

    @staticmethod
    def from_row(d):
        return Rec(icao=d["icao"], first_seen=int(d["first_seen"]),
                   last_seen=int(d["last_seen"]), callsign=d.get("callsign", ""),
                   first_lat=float(d["first_lat"]), first_lon=float(d["first_lon"]),
                   best_lat=float(d["best_lat"]), best_lon=float(d["best_lon"]),
                   best_alt_ft=int(float(d["best_alt_ft"])),
                   best_range_nm=float(d["best_range_nm"]),
                   max_alt_ft=int(float(d["max_alt_ft"])),
                   max_rssi=float(d["max_rssi"]), polls=int(d["polls"]),
                   clock=d.get("clock", "ok") or "ok")


def merge_rec(a, b):
    """Fold record b into a (same ICAO)."""
    if b.first_seen < a.first_seen:
        a.first_seen, a.first_lat, a.first_lon = b.first_seen, b.first_lat, b.first_lon
    a.last_seen = max(a.last_seen, b.last_seen)
    a.callsign = a.callsign or b.callsign
    a.max_alt_ft = max(a.max_alt_ft, b.max_alt_ft)
    a.max_rssi = max(a.max_rssi, b.max_rssi)
    a.polls += b.polls
    if b.best_range_nm > a.best_range_nm:
        a.best_lat, a.best_lon = b.best_lat, b.best_lon
        a.best_alt_ft, a.best_range_nm = b.best_alt_ft, b.best_range_nm
    if "corrected" in (a.clock, b.clock):
        a.clock = "corrected"
    elif "unsynced" in (a.clock, b.clock):
        a.clock = "unsynced"
    return a


def atomic_write_csv(path, header, rows):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class TimedatectlClock:
    """True when systemd reports the clock is NTP-synchronised (cached)."""

    def __init__(self, ttl=30.0):
        self.ttl = ttl
        self._t = None
        self._v = True

    def __call__(self):
        now = time.monotonic()
        if self._t is not None and now - self._t < self.ttl:
            return self._v
        self._t = now
        try:
            out = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True, text=True, timeout=5).stdout.strip().lower()
            if out in ("yes", "no"):
                self._v = (out == "yes")
        except Exception:
            pass  # cannot tell: keep last known value (initially True)
        return self._v


class Logger:
    def __init__(self, a, time_fn=time.time, mono_fn=time.monotonic, clock_fn=None):
        self.a = a
        self.time_fn = time_fn
        self.mono_fn = mono_fn
        if clock_fn is not None:
            self.clock_fn = clock_fn
        elif a.clock_mode == "synced":
            self.clock_fn = lambda: True
        elif a.clock_mode == "unsynced":
            self.clock_fn = lambda: False
        else:
            self.clock_fn = TimedatectlClock()
        self.master_path = os.path.join(a.data_dir, "adsb_aircraft.csv")
        self.summary_dir = os.path.join(a.data_dir, "summary")
        os.makedirs(self.summary_dir, exist_ok=True)

        self.known = self._load_known()          # all-time ICAOs in master CSV
        self.new_rows = []                       # batched, not yet on disk
        self.unsynced_master = set()             # ICAOs whose master row is unsynced
        self.days = {}                           # day -> {icao: Rec}
        self.dirty_days = set()
        self.station = self._station_from_args()
        self.last_offset = None
        self.prev_synced = None
        self.post_step = False
        self.last_new_icao = None
        self.last_new_ts = None
        self.stale_warned = 0.0
        self.prune_summaries()

    # ---------- setup helpers ----------
    def _station_from_args(self):
        if self.a.lat is not None and self.a.lon is not None:
            return (self.a.lat, self.a.lon)
        return self._station_from_receiver_json()

    def _station_from_receiver_json(self):
        try:
            with open(self.a.receiver_json) as f:
                r = json.load(f)
            if "lat" in r and "lon" in r:
                return (float(r["lat"]), float(r["lon"]))
        except Exception:
            pass
        return None

    def _load_known(self):
        known = set()
        try:
            with open(self.master_path, newline="", encoding="utf-8") as f:
                rd = csv.reader(f)
                next(rd, None)
                for row in rd:
                    if len(row) > 1 and row[1]:
                        known.add(row[1].upper())
        except FileNotFoundError:
            pass
        return known

    def _summary_path(self, day):
        return os.path.join(self.summary_dir, f"summary_{day}.csv")

    def _day(self, day):
        if day not in self.days:
            self.days[day] = self._read_summary(day)
        return self.days[day]

    def _read_summary(self, day):
        path = self._summary_path(day)
        recs = {}
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        r = Rec.from_row(row)
                        recs[r.icao] = r
                    except (KeyError, ValueError, TypeError):
                        continue
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"summary {path} unreadable ({e}); moving aside")
            try:
                os.replace(path, path + ".corrupt")
            except OSError:
                pass
        return recs

    def prune_summaries(self):
        keep = self.a.keep_days
        if keep <= 0:
            return
        cutoff = utc_day(self.time_fn() - keep * 86400)
        try:
            for name in os.listdir(self.summary_dir):
                if name.startswith("summary_") and name.endswith(".csv"):
                    day = name[len("summary_"):-len(".csv")]
                    if day < cutoff:
                        os.unlink(os.path.join(self.summary_dir, name))
        except OSError:
            pass

    # ---------- input ----------
    def _read_json(self):
        try:
            with open(self.a.aircraft_json) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        t = data.get("now")
        if isinstance(t, (int, float)) and abs(self.time_fn() - t) > self.a.stale_after:
            if self.mono_fn() - self.stale_warned > 300:
                self.stale_warned = self.mono_fn()
                log("aircraft.json looks stale (decoder stopped or clock stepped)")
            return None
        return data

    def _parse_ac(self, ac):
        hx = str(ac.get("hex", "")).strip().upper()
        if len(hx) != 6 or hx.startswith("~"):
            return None
        lat, lon = ac.get("lat"), ac.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None
        if (lat == 0 and lon == 0) or abs(lat) > 90 or abs(lon) > 180:
            return None
        sp = ac.get("seen_pos")
        if isinstance(sp, (int, float)) and sp > self.a.max_pos_age:
            return None
        alt = ac.get("alt_baro")
        if alt == "ground":
            alt = 0
        elif not isinstance(alt, (int, float)):
            alt = ac.get("alt_geom")
            if not isinstance(alt, (int, float)):
                alt = 0
        gs = ac.get("gs")
        trk = ac.get("track")
        rssi = ac.get("rssi")
        return {
            "icao": hx,
            "callsign": str(ac.get("flight", "")).strip(),
            "lat": float(lat), "lon": float(lon),
            "alt": int(alt),
            "gs": int(gs) if isinstance(gs, (int, float)) else 0,
            "trk": int(trk) if isinstance(trk, (int, float)) else 0,
            "squawk": str(ac.get("squawk", "") or ""),
            "rssi": float(rssi) if isinstance(rssi, (int, float)) else -99.9,
        }

    # ---------- clock ----------
    def _clock_state(self):
        if self.post_step or self.clock_fn():
            return "ok"
        return "unsynced"

    def _check_clock(self):
        offset = self.time_fn() - self.mono_fn()
        synced = bool(self.clock_fn())
        stepped = False
        if self.last_offset is not None:
            delta = offset - self.last_offset
            if abs(delta) > STEP_THRESHOLD:
                self.apply_step(delta)
                stepped = True
        if (not stepped and self.prev_synced is False and synced):
            self.apply_step(0.0)  # synced without a visible jump: just relabel
        self.last_offset = offset
        self.prev_synced = synced

    def apply_step(self, delta):
        """Clock jumped by delta seconds: fix rows recorded while unsynced."""
        delta = int(round(delta))
        self.post_step = True
        if self.unsynced_master:
            self.flush_master()
            self._shift_master(self.unsynced_master, delta)
            n = len(self.unsynced_master)
            self.unsynced_master = set()
            log(f"clock step {delta:+d}s: corrected {n} master rows")
        moved = []
        for day in list(self.days):
            recs = self.days[day]
            for icao in [i for i, r in recs.items() if r.clock == "unsynced"]:
                moved.append(recs.pop(icao))
                self.dirty_days.add(day)
        for r in moved:
            r.first_seen += delta
            r.last_seen += delta
            r.clock = "corrected"
            nd = utc_day(r.first_seen)
            tgt = self._day(nd)
            if r.icao in tgt:
                merge_rec(tgt[r.icao], r)
            else:
                tgt[r.icao] = r
            self.dirty_days.add(nd)

    def _shift_master(self, icaos, delta):
        rows = []
        try:
            with open(self.master_path, newline="", encoding="utf-8") as f:
                rd = csv.reader(f)
                header = next(rd, GAME_HEADER)
                for row in rd:
                    if len(row) > 1 and row[1].upper() in icaos:
                        try:
                            row[0] = str(int(float(row[0])) + delta)
                        except ValueError:
                            pass
                    rows.append(row)
        except FileNotFoundError:
            return
        atomic_write_csv(self.master_path, header, rows)

    # ---------- core ----------
    def poll_once(self):
        self._check_clock()
        data = self._read_json()
        if data is None:
            return 0
        ts = int(self.time_fn())
        state = self._clock_state()
        seen = 0
        for ac in data.get("aircraft", []):
            p = self._parse_ac(ac)
            if p:
                self._observe(p, ts, state)
                seen += 1
        return seen

    def _observe(self, p, ts, state):
        icao = p["icao"]
        day = utc_day(ts)
        recs = self._day(day)
        rng = (haversine_nm(self.station[0], self.station[1], p["lat"], p["lon"])
               if self.station else -1.0)
        r = recs.get(icao)
        if r is None:
            r = Rec(icao=icao, first_seen=ts, last_seen=ts,
                    callsign=p["callsign"], first_lat=p["lat"], first_lon=p["lon"],
                    best_lat=p["lat"], best_lon=p["lon"], best_alt_ft=p["alt"],
                    best_range_nm=rng, max_alt_ft=p["alt"], max_rssi=p["rssi"],
                    polls=0, clock=state)
            recs[icao] = r
        r.last_seen = max(r.last_seen, ts)
        r.polls += 1
        r.callsign = r.callsign or p["callsign"]
        r.max_alt_ft = max(r.max_alt_ft, p["alt"])
        r.max_rssi = max(r.max_rssi, p["rssi"])
        if rng > r.best_range_nm:
            r.best_lat, r.best_lon = p["lat"], p["lon"]
            r.best_alt_ft, r.best_range_nm = p["alt"], rng
        if state == "unsynced" and r.clock == "ok":
            r.clock = "unsynced"
        self.dirty_days.add(day)

        if icao not in self.known:
            self.known.add(icao)
            self.new_rows.append([ts, icao, p["callsign"], f"{p['lat']:.6f}",
                                  f"{p['lon']:.6f}", p["alt"], p["gs"], p["trk"],
                                  p["squawk"]])
            self.last_new_icao, self.last_new_ts = icao, ts
            if state == "unsynced":
                self.unsynced_master.add(icao)

    # ---------- output ----------
    def flush_master(self):
        if not self.new_rows:
            return 0
        new = not os.path.exists(self.master_path)
        os.makedirs(self.a.data_dir, exist_ok=True)
        with open(self.master_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            if new:
                w.writerow(GAME_HEADER)
            w.writerows(self.new_rows)
            f.flush()
            os.fsync(f.fileno())
        n = len(self.new_rows)
        self.new_rows = []
        log(f"flush: +{n} new ICAO (all-time {len(self.known)})")
        return n

    def flush_summary(self):
        today = utc_day(self.time_fn())
        for day in sorted(self.dirty_days):
            recs = self.days.get(day, {})
            path = self._summary_path(day)
            if recs:
                rows = [r.to_row() for r in sorted(recs.values(),
                                                   key=lambda x: x.first_seen)]
                atomic_write_csv(path, SUMMARY_COLS, rows)
            elif os.path.exists(path):
                os.unlink(path)
        self.dirty_days = set()
        for day in [d for d in self.days if d < today]:
            del self.days[day]
        if self.station is None:
            self.station = self._station_from_receiver_json()

    def write_status(self):
        path = self.a.status_file
        if not path or not os.path.isdir(os.path.dirname(path)):
            return
        today = utc_day(self.time_fn())
        st = {"updated": int(self.time_fn()),
              "all_time_unique": len(self.known),
              "today_unique": len(self.days.get(today, {})),
              "last_new_icao": self.last_new_icao,
              "last_new_ts": self.last_new_ts,
              "clock": self._clock_state()}
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".st_")
            with os.fdopen(fd, "w") as f:
                json.dump(st, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def flush_all(self):
        self.flush_master()
        self.flush_summary()
        self.write_status()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--aircraft-json", default="/run/dump1090-fa/aircraft.json")
    p.add_argument("--receiver-json", default="/run/dump1090-fa/receiver.json")
    p.add_argument("--data-dir", default="/var/lib/adsb-zero")
    p.add_argument("--lat", type=float, default=None, help="station latitude")
    p.add_argument("--lon", type=float, default=None, help="station longitude")
    p.add_argument("--poll", type=float, default=5.0, help="seconds between polls")
    p.add_argument("--flush", type=float, default=60.0,
                   help="seconds between batched appends of new ICAOs")
    p.add_argument("--summary-flush", type=float, default=600.0,
                   help="seconds between summary rewrites")
    p.add_argument("--max-pos-age", type=float, default=15.0,
                   help="ignore positions older than this many seconds")
    p.add_argument("--stale-after", type=float, default=30.0)
    p.add_argument("--keep-days", type=int, default=400,
                   help="delete summary files older than this (0 = keep all)")
    p.add_argument("--status-file", default="/run/adsb-zero/status.json")
    p.add_argument("--clock-mode", choices=["auto", "synced", "unsynced"],
                   default="auto", help="auto uses timedatectl; others for testing")
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    lg = Logger(a)
    stop = threading.Event()

    def _sig(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log(f"start: {len(lg.known)} ICAOs already logged; station="
        f"{lg.station if lg.station else 'unknown (best = first position)'}")
    last_m = last_s = lg.mono_fn()
    while not stop.is_set():
        try:
            lg.poll_once()
            now = lg.mono_fn()
            if now - last_m >= a.flush:
                lg.flush_master()
                lg.write_status()
                last_m = now
            if now - last_s >= a.summary_flush:
                lg.flush_summary()
                lg.prune_summaries()
                last_s = now
        except Exception as e:  # keep running; journal shows the problem
            log(f"error: {e!r}")
        stop.wait(a.poll)
    lg.flush_all()
    log("stopped, flushed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
