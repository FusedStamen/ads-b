#!/usr/bin/env python3
"""Tests for adsb_unique_logger.py using synthetic aircraft.json data.
Run:  python3 test_adsb_logger.py
"""
import csv
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adsb_unique_logger as L  # noqa: E402

REAL_TS = 1791923995  # same epoch range as the game's own CSV sample


def game_parse(csv_path):
    """Replica of WatchDogsGo plugins/wardrive_upload.py _parse_adsb (as pasted)."""
    aircraft = {}
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        for parts in reader:
            if len(parts) < 8:
                continue
            try:
                lat = float(parts[3]) if parts[3] else 0.0
                lon = float(parts[4]) if parts[4] else 0.0
            except ValueError:
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                continue
            icao = parts[1]
            if not icao or icao in aircraft:
                continue
            try:
                alt = int(float(parts[5])) if parts[5] else 0
                spd = int(float(parts[6])) if parts[6] else 0
                hdg = int(float(parts[7])) if parts[7] else 0
            except ValueError:
                alt = spd = hdg = 0
            aircraft[icao] = {"icao": icao, "callsign": parts[2], "lat": lat,
                              "lon": lon, "alt_ft": alt, "speed_kt": spd,
                              "heading": hdg, "first_seen": parts[0], "type": "ADSB"}
    return list(aircraft.values())


class Fake:
    def __init__(self, t):
        self.t = float(t)
        self.m = 500.0
        self.synced = True

    def time(self):
        return self.t

    def mono(self):
        return self.m

    def advance(self, dt):
        self.t += dt
        self.m += dt

    def step(self, delta):
        self.t += delta


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.json = os.path.join(self.dir, "aircraft.json")
        self.data = os.path.join(self.dir, "data")
        self.fake = Fake(REAL_TS)

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kw):
        argv = ["--aircraft-json", self.json, "--receiver-json",
                os.path.join(self.dir, "none.json"), "--data-dir", self.data,
                "--status-file", os.path.join(self.dir, "st", "status.json")]
        for k, v in kw.items():
            argv += [f"--{k.replace('_', '-')}", str(v)]
        return L.parse_args(argv)

    def logger(self, fake_clock=True, **kw):
        a = self.args(**kw)
        if fake_clock:
            return L.Logger(a, time_fn=self.fake.time, mono_fn=self.fake.mono,
                            clock_fn=lambda: self.fake.synced)
        return L.Logger(a)

    def feed(self, aircraft):
        with open(self.json, "w") as f:
            json.dump({"now": self.fake.t, "messages": 1, "aircraft": aircraft}, f)

    def master_rows(self):
        with open(os.path.join(self.data, "adsb_aircraft.csv"), newline="") as f:
            return list(csv.reader(f))

    def summary(self, day):
        p = os.path.join(self.data, "summary", f"summary_{day}.csv")
        with open(p, newline="") as f:
            return {r["icao"]: r for r in csv.DictReader(f)}


class TestFiltering(Base):
    def test_only_valid_positions_logged_in_game_format(self):
        lg = self.logger(lat=41.50, lon=-71.50)
        self.feed([
            {"hex": "a1b2c3", "flight": "UAL123  ", "lat": 42.5, "lon": -71.4,
             "alt_baro": 12000, "gs": 250.4, "track": 270.2, "squawk": "1200",
             "rssi": -15.0, "seen_pos": 0.4},
            {"hex": "AAAAAA", "messages": 5},                              # no position
            {"hex": "~123456", "lat": 42.0, "lon": -71.0},                 # TIS-B style
            {"hex": "BBBBBB", "lat": 0, "lon": 0},                         # zero fix
            {"hex": "CCCCCC", "lat": 42.0, "lon": -71.0, "seen_pos": 60},  # stale pos
            {"hex": "dddddd", "lat": 42.4, "lon": -71.2, "alt_baro": "ground",
             "seen_pos": 1},
            {"hex": "eeeeee", "lat": 95.0, "lon": -71.0},                  # out of range
        ])
        lg.poll_once()
        lg.flush_all()
        rows = self.master_rows()
        self.assertEqual(rows[0], L.GAME_HEADER)
        self.assertEqual([r[1] for r in rows[1:]], ["A1B2C3", "DDDDDD"])
        r = rows[1]
        self.assertEqual(r, [str(REAL_TS), "A1B2C3", "UAL123", "42.500000",
                             "-71.400000", "12000", "250", "270", "1200"])
        self.assertEqual(rows[2][5], "0")  # ground -> 0
        self.assertEqual(len(game_parse(os.path.join(self.data, "adsb_aircraft.csv"))), 2)

    def test_dedup_within_run_and_across_restart(self):
        ac = {"hex": "a1b2c3", "lat": 42.5, "lon": -71.4, "alt_baro": 9000}
        lg = self.logger()
        self.feed([ac])
        for _ in range(5):
            self.fake.advance(5)
            self.feed([ac])
            lg.poll_once()
        lg.flush_all()
        self.assertEqual(len(self.master_rows()), 2)
        lg2 = self.logger()  # restart
        self.feed([ac, {"hex": "ffffff", "lat": 41.0, "lon": -70.0}])
        lg2.poll_once()
        lg2.flush_all()
        ids = [r[1] for r in self.master_rows()[1:]]
        self.assertEqual(ids, ["A1B2C3", "FFFFFF"])

    def test_no_writes_until_flush(self):
        lg = self.logger()
        self.feed([{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4}])
        lg.poll_once()
        self.assertFalse(os.path.exists(os.path.join(self.data, "adsb_aircraft.csv")))
        lg.flush_master()
        self.assertTrue(os.path.exists(os.path.join(self.data, "adsb_aircraft.csv")))


class TestSummary(Base):
    def test_first_last_best_position(self):
        lg = self.logger(lat=41.50, lon=-71.50)
        track = [(42.40, -71.20, 5000), (42.80, -72.00, 21000), (42.50, -71.50, 9000)]
        for i, (la, lo, alt) in enumerate(track):
            self.fake.advance(10)
            self.feed([{"hex": "a1b2c3", "flight": "DAL9", "lat": la, "lon": lo,
                        "alt_baro": alt, "rssi": -20 + i}])
            lg.poll_once()
        lg.flush_all()
        day = L.utc_day(REAL_TS)
        s = self.summary(day)["A1B2C3"]
        self.assertEqual((s["first_lat"], s["first_lon"]), ("42.400000", "-71.200000"))
        self.assertEqual((s["best_lat"], s["best_lon"]), ("42.800000", "-72.000000"))
        self.assertEqual(s["best_alt_ft"], "21000")
        self.assertEqual(s["max_alt_ft"], "21000")
        self.assertEqual(s["max_rssi"], "-18.0")
        self.assertEqual(s["polls"], "3")
        self.assertEqual(int(s["last_seen"]) - int(s["first_seen"]), 20)
        self.assertGreater(float(s["best_range_nm"]), 30)
        # master row still holds the FIRST position
        self.assertEqual(self.master_rows()[1][3], "42.400000")

    def test_day_rollover_gives_two_files_one_master_row(self):
        lg = self.logger()
        ac = [{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4}]
        self.fake.t = 1791935990  # shortly before a UTC midnight? compute below
        # move to 30 s before the next UTC midnight
        nxt = (int(self.fake.t) // 86400 + 1) * 86400
        self.fake.t = nxt - 30
        self.feed(ac)
        lg.poll_once()
        self.fake.advance(60)
        self.feed(ac)
        lg.poll_once()
        lg.flush_all()
        d1, d2 = L.utc_day(nxt - 30), L.utc_day(nxt + 30)
        self.assertNotEqual(d1, d2)
        self.assertIn("A1B2C3", self.summary(d1))
        self.assertIn("A1B2C3", self.summary(d2))
        self.assertEqual(len(self.master_rows()), 2)

    def test_prune_old_summaries(self):
        os.makedirs(os.path.join(self.data, "summary"))
        old = os.path.join(self.data, "summary", "summary_2020-01-01.csv")
        new = os.path.join(self.data, "summary", f"summary_{L.utc_day(REAL_TS)}.csv")
        for p in (old, new):
            open(p, "w").write("icao\n")
        self.logger(keep_days=30)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))


class TestClock(Base):
    def test_offline_boot_then_ntp_jump_corrects_timestamps(self):
        wrong = 1_000_000
        self.fake.t = wrong
        self.fake.synced = False
        lg = self.logger()
        self.feed([{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4}])
        lg.poll_once()
        self.fake.advance(30)
        self.feed([{"hex": "d4e5f6", "lat": 42.6, "lon": -71.5}])
        lg.poll_once()
        lg.flush_all()
        old_day = L.utc_day(wrong)
        self.assertEqual(self.summary(old_day)["A1B2C3"]["clock"], "unsynced")
        # NTP sync: wall clock jumps to the real time, monotonic keeps going
        real_now = REAL_TS
        self.fake.advance(5)
        self.fake.step(real_now - self.fake.t)
        self.fake.synced = True
        self.feed([{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4},
                   {"hex": "112233", "lat": 42.7, "lon": -71.6}])
        lg.poll_once()
        lg.flush_all()
        rows = {r[1]: r for r in self.master_rows()[1:]}
        # A1B2C3 first seen at `wrong`, corrected by the jump (35 s of real time passed)
        self.assertEqual(int(rows["A1B2C3"][0]), real_now - 35)
        self.assertEqual(int(rows["D4E5F6"][0]), real_now - 5)
        self.assertEqual(int(rows["112233"][0]), real_now)
        new_day = L.utc_day(real_now)
        s = self.summary(new_day)
        self.assertEqual(s["A1B2C3"]["clock"], "corrected")
        self.assertEqual(s["112233"]["clock"], "ok")
        self.assertFalse(os.path.exists(
            os.path.join(self.data, "summary", f"summary_{old_day}.csv")))

    def test_synced_without_jump_just_relabels(self):
        self.fake.synced = False
        lg = self.logger()
        self.feed([{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4}])
        lg.poll_once()
        self.fake.advance(10)
        self.fake.synced = True
        self.feed([])
        lg.poll_once()
        lg.flush_all()
        day = L.utc_day(REAL_TS)
        self.assertEqual(self.summary(day)["A1B2C3"]["clock"], "corrected")
        self.assertEqual(int(self.master_rows()[1][0]), REAL_TS)

    def test_always_synced_stays_ok(self):
        lg = self.logger()
        self.feed([{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4}])
        lg.poll_once()
        lg.flush_all()
        self.assertEqual(self.summary(L.utc_day(REAL_TS))["A1B2C3"]["clock"], "ok")

    def test_stale_json_ignored(self):
        lg = self.logger()
        self.feed([{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4}])
        self.fake.advance(120)  # decoder died 2 minutes ago
        lg.poll_once()
        lg.flush_all()
        self.assertFalse(os.path.exists(os.path.join(self.data, "adsb_aircraft.csv")))


class TestCLI(Base):
    def test_cli_runs_batches_and_flushes_on_sigterm(self):
        ac = [{"hex": "a1b2c3", "lat": 42.5, "lon": -71.4, "alt_baro": 8000,
               "gs": 300, "track": 90, "seen_pos": 0.2},
              {"hex": "d4e5f6", "lat": 42.6, "lon": -71.5, "alt_baro": 31000}]
        with open(self.json, "w") as f:
            json.dump({"now": time.time(), "aircraft": ac}, f)
        p = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "adsb_unique_logger.py"),
             "--aircraft-json", self.json, "--data-dir", self.data,
             "--receiver-json", os.path.join(self.dir, "none.json"),
             "--status-file", os.path.join(self.dir, "st", "s.json"),
             "--poll", "0.2", "--flush", "600", "--summary-flush", "600",
             "--clock-mode", "synced"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(1.5)
        # nothing flushed yet (flush interval is long): batching works
        self.assertFalse(os.path.exists(os.path.join(self.data, "adsb_aircraft.csv")))
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("stopped, flushed", out)
        self.assertEqual(len(game_parse(os.path.join(self.data, "adsb_aircraft.csv"))), 2)
        self.assertTrue(os.path.isdir(os.path.join(self.data, "summary")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
