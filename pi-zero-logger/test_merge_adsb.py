#!/usr/bin/env python3
"""Tests for merge_adsb.py on synthetic data.  Run: python3 test_merge_adsb.py"""
import csv
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_adsb_logger import game_parse  # noqa: E402
import merge_adsb as M  # noqa: E402

HDR = "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n"


def w(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        f.write(HDR + "".join(l + "\n" for l in lines))


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        # uConsole: big multi-row file, some junk rows, lowercase ICAO, zero fix first
        w(f"{self.d}/loot/2026-10-13_16-37-35/adsb_aircraft.csv", [
            "1791923995,C06357,,0.000000,0.000000,0,0,0,",          # zero fix: invalid
            "1791924000,C06357,,42.349640,-71.019870,975,0,0,",
            "1791924010,C06357,,42.350000,-71.020000,1100,175,204,",
            "1791924020,a1b2c3,UAL1,42.5,-71.4,9000,250,270,1200",  # lowercase
            "1791924030,BADROW,x",                                   # too short
            "1791924040,DEAD01,,abc,def,1,1,1,",                     # non-numeric
            "1791924050,DEAD02,,95.0,10.0,1,1,1,",                   # out of range
        ])
        # Zero: earlier sighting of A1B2C3 and a new aircraft
        w(f"{self.d}/zero/adsb_aircraft.csv", [
            "1791923000,A1B2C3,UAL1,42.6,-71.5,12000,260,265,1200",
            "1791923100,F00D01,,41.9,-70.9,30000,450,90,",
        ])
        self.zero = f"{self.d}/zero/adsb_aircraft.csv"
        self.uc = f"{self.d}/loot/2026-10-13_16-37-35"

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_earliest_row_wins_and_validity(self):
        out = f"{self.d}/out.csv"
        rc = M.main([self.zero, self.uc, "--out-file", out])
        self.assertEqual(rc, 0)
        with open(out, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], M.HEADER)
        by = {r[1]: r for r in rows[1:]}
        self.assertEqual(sorted(by), ["A1B2C3", "C06357", "F00D01"])
        self.assertEqual(by["A1B2C3"][0], "1791923000")          # Zero was earlier
        self.assertEqual(by["A1B2C3"][3], "42.6")
        self.assertEqual(by["C06357"][0], "1791924000")          # zero-fix row skipped
        self.assertTrue(all(len(r) == 9 for r in rows))
        ts = [int(r[0]) for r in rows[1:]]
        self.assertEqual(ts, sorted(ts))
        # the game's own parser accepts every row and gets the same ICAO set
        parsed = game_parse(out)
        self.assertEqual(sorted(p["icao"] for p in parsed), ["A1B2C3", "C06357", "F00D01"])

    def test_report_counts_only_here(self):
        out = f"{self.d}/out.csv"
        r = subprocess.run([sys.executable, os.path.join(HERE, "merge_adsb.py"),
                            self.zero, self.uc, "--out-file", out],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wrote 3 unique ICAOs", r.stdout)
        # Zero: A1B2C3 (shared), F00D01 (only Zero) -> 1 only here
        self.assertRegex(r.stdout, r"\[0\].*2 unique valid ICAOs, 1 seen only here")
        # uConsole: A1B2C3 (shared), C06357 (only uConsole) -> 1 only here
        self.assertRegex(r.stdout, r"\[1\].*2 unique valid ICAOs, 1 seen only here")

    def test_out_dir_creates_session_folder(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "merge_adsb.py"),
                            self.zero, self.uc, "--out-dir", f"{self.d}/loot"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        sess = [x for x in os.listdir(f"{self.d}/loot")
                if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", x)
                and x != "2026-10-13_16-37-35"]
        self.assertEqual(len(sess), 1)
        self.assertTrue(os.path.isfile(f"{self.d}/loot/{sess[0]}/adsb_aircraft.csv"))

    def test_skip_file_stages_only_new_icaos(self):
        loot = f"{self.d}/stage_loot"
        skip = f"{self.d}/state/staged_icaos.txt"
        os.makedirs(loot)

        def run():
            return subprocess.run([sys.executable, os.path.join(HERE, "merge_adsb.py"),
                                   self.zero, "--out-dir", loot, "--skip-file", skip],
                                  capture_output=True, text=True)

        def sessions():
            return sorted(x for x in os.listdir(loot))

        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        s1 = sessions()
        self.assertEqual(len(s1), 1)
        with open(f"{loot}/{s1[0]}/adsb_aircraft.csv") as f:
            self.assertEqual(sorted(r[1] for r in list(csv.reader(f))[1:]),
                             ["A1B2C3", "F00D01"])
        with open(skip) as f:
            self.assertEqual(f.read().split(), ["A1B2C3", "F00D01"])
        # second run, nothing new: no folder created
        r = run()
        self.assertIn("no new ICAOs (2 already staged)", r.stdout)
        self.assertEqual(sessions(), s1)
        # Zero logs one more aircraft: only it is staged, in a new folder
        with open(self.zero, "a") as f:
            f.write("1791925000,BEEF99,,43.0,-72.0,20000,400,45,\n")
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        s2 = sessions()
        self.assertEqual(len(s2), 2)
        new = [x for x in s2 if x not in s1][0]
        with open(f"{loot}/{new}/adsb_aircraft.csv") as f:
            self.assertEqual([r[1] for r in list(csv.reader(f))[1:]], ["BEEF99"])
        # each staged folder is something the game's uploader would pick up
        for s in s2:
            self.assertTrue(game_parse(f"{loot}/{s}/adsb_aircraft.csv"))
        with open(skip) as f:
            self.assertEqual(f.read().split(), ["A1B2C3", "F00D01", "BEEF99"])

    def test_session_folder_name_never_reused(self):
        loot = f"{self.d}/collide"
        for _ in range(3):  # three runs inside the same second
            self.assertEqual(M.main([self.zero, "--out-dir", loot]), 0)
        names = os.listdir(loot)
        self.assertEqual(len(names), 3)
        for n in names:
            self.assertTrue(os.path.isfile(f"{loot}/{n}/adsb_aircraft.csv"))

    def test_pull_script_stage_with_stand_in_rsync(self):
        shim = f"{self.d}/bin"
        os.makedirs(shim)
        with open(f"{shim}/rsync", "w") as f:
            f.write('#!/usr/bin/env bash\nsrc="${@: -2:1}"; dst="${@: -1}"; cp -r "$src". "$dst"\n')
        os.chmod(f"{shim}/rsync", 0o755)
        env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}",
                   ZERO_SRC=f"{self.d}/zero/", DEST=f"{self.d}/pull",
                   LOOT=f"{self.d}/pl_loot")
        os.makedirs(env["LOOT"])
        run = lambda *a: subprocess.run(
            ["bash", os.path.join(HERE, "pull_zero.sh"), *a],
            capture_output=True, text=True, env=env)
        r = run("--stage")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wrote 2 unique ICAOs", r.stdout)
        self.assertEqual(len(os.listdir(env["LOOT"])), 1)
        r = run("--stage")
        self.assertIn("no new ICAOs", r.stdout)
        self.assertEqual(len(os.listdir(env["LOOT"])), 1)
        # default mode still only reports, never touches loot
        r = run(self.uc)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(f"{self.d}/pull/merged_adsb_aircraft.csv"))
        self.assertEqual(len(os.listdir(env["LOOT"])), 1)

    def test_missing_input_fails_cleanly(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "merge_adsb.py"),
                            f"{self.d}/nope", "--out-file", f"{self.d}/o.csv"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(f"{self.d}/o.csv"))

    def test_large_file_streams(self):
        big = f"{self.d}/big/adsb_aircraft.csv"
        os.makedirs(os.path.dirname(big))
        with open(big, "w") as f:
            f.write(HDR)
            for i in range(300000):
                icao = f"{(i % 2500) + 0x100000:06X}"
                f.write(f"{1791920000 + i},{icao},,42.{i % 1000:03d},-71.{i % 999:03d},"
                        f"{i % 40000},{i % 500},{i % 360},\n")
        out = f"{self.d}/big_out.csv"
        self.assertEqual(M.main([big, "--out-file", out]), 0)
        with open(out) as f:
            self.assertEqual(sum(1 for _ in f), 2501)


if __name__ == "__main__":
    unittest.main(verbosity=2)
