#!/usr/bin/env python3
"""Tests for validate_for_game.py. The validator must agree with the game's own parser
replica on which aircraft are uploadable.  Run: python3 test_validate_for_game.py"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_for_game as V  # noqa: E402
from test_adsb_logger import game_parse  # noqa: E402

HDR = "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n"
NOW = 1_800_000_000


def write(path, lines, header=HDR):
    with open(path, "w", newline="") as f:
        f.write(header + "".join(l + "\n" for l in lines))


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = f"{self.tmp.name}/adsb_aircraft.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_file_is_ok_and_matches_game_parser(self):
        write(self.p, [f"{NOW},A1B2C3,UAL1,42.5,-71.4,9000,250,270,1200",
                       f"{NOW + 5},D4E5F6,,41.9,-70.9,31000,450,90,"])
        r = V.analyze([self.p], now=NOW + 100)
        self.assertEqual(sorted(r["uploadable"]), ["A1B2C3", "D4E5F6"])
        self.assertEqual(sorted(a["icao"] for a in game_parse(self.p)), sorted(r["uploadable"]))
        text = V.report(r)
        self.assertIn("AIRCRAFT THE GAME WOULD UPLOAD: 2", text)
        self.assertIn("VERDICT: OK", text)

    def test_every_skip_reason_counted_and_agrees_with_game(self):
        write(self.p, [
            f"{NOW},GOOD01,,42.5,-71.4,1,1,1,",
            f"{NOW},SHORT,x",
            f"{NOW},ZERO01,,0,0,1,1,1,",
            f"{NOW},BLANK1,,,,1,1,1,",
            f"{NOW},NONNUM,,abc,def,1,1,1,",
            f"{NOW},OUTLAT,,95.0,10.0,1,1,1,",
            f"{NOW},OUTLON,,10.0,190.0,1,1,1,",
            f"{NOW},,,42.5,-71.4,1,1,1,",
            f"{NOW + 9},GOOD01,,43.0,-72.0,2,2,2,",
        ])
        r = V.analyze([self.p], now=NOW + 100)
        self.assertEqual(list(r["uploadable"]), ["GOOD01"])
        self.assertEqual(r["uploadable"]["GOOD01"]["lat"], 42.5)   # first row kept
        self.assertEqual(r["skipped"], {"short_row": 1, "zero_or_blank_position": 2,
                                        "non_numeric_position": 1, "position_out_of_range": 2,
                                        "empty_icao": 1})
        self.assertEqual(r["dup_rows"], 1)
        self.assertEqual(len(game_parse(self.p)), 1)

    def test_warnings_for_case_timestamps_and_icao_format(self):
        write(self.p, [
            f"{NOW},a1b2c3,,42.5,-71.4,1,1,1,",
            f"{NOW},A1B2C3,,42.6,-71.5,1,1,1,",          # same ICAO, other case
            "1000000,D4E5F6,,42.5,-71.4,1,1,1,",           # clock was wrong
            f"{NOW + 10 ** 7},D4E5F7,,42.5,-71.4,1,1,1,",  # far future
            f"{NOW},XYZ,,42.5,-71.4,1,1,1,",               # not hex
            f"{NOW},FFFFFF,,42.5,-71.4,99999,1,1,",        # silly altitude
        ])
        r = V.analyze([self.p], now=NOW + 100)
        text = V.report(r)
        self.assertEqual(r["lower_icao"], 1)
        self.assertEqual(r["case_variants"], 1)
        self.assertEqual(r["bad_ts"], 2)
        self.assertEqual(r["bad_alt"], 1)
        self.assertEqual(r["bad_icao"], ["XYZ"])
        self.assertIn("WARNINGS", text)
        self.assertIn("lower case", text)
        self.assertIn("timestamps before 2024", text)
        self.assertNotIn("VERDICT: OK", text)

    def test_wrong_header_and_empty_file(self):
        write(self.p, [], header="a,b,c\n")
        r = V.analyze([self.p], now=NOW)
        self.assertFalse(r["header_ok"])
        self.assertIn("nothing would be uploaded", V.report(r))

    def test_folder_input_finds_session_files(self):
        sess = f"{self.tmp.name}/2026-10-31_19-00-00"
        os.makedirs(sess)
        write(f"{sess}/adsb_aircraft.csv", [f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,"])
        self.assertEqual(V.find_csvs(self.tmp.name), [f"{sess}/adsb_aircraft.csv"])

    def test_cli_runs_and_saves_report(self):
        import subprocess
        write(self.p, [f"{int(__import__('time').time())},A1B2C3,,42.5,-71.4,1,1,1,"])
        r = subprocess.run([sys.executable, os.path.join(HERE, "validate_for_game.py"), self.p],
                           capture_output=True, text=True,
                           env=dict(os.environ, HOME=self.tmp.name))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("AIRCRAFT THE GAME WOULD UPLOAD: 1", r.stdout)
        self.assertTrue(os.path.isfile(f"{self.tmp.name}/game_validation.txt"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "validate_for_game.py"),
                            f"{self.tmp.name}/nope.csv"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)

    def test_logger_output_passes_validator(self):
        """End to end: what adsb_unique_logger writes is accepted by the validator with no warnings."""
        import json
        import adsb_unique_logger as L
        d = self.tmp.name
        now = [float(NOW)]
        aj = f"{d}/aircraft.json"
        args = L.parse_args(["--aircraft-json", aj, "--receiver-json", f"{d}/none.json",
                             "--data-dir", f"{d}/data", "--status-file", "",
                             "--clock-mode", "synced"])
        lg = L.Logger(args, time_fn=lambda: now[0], mono_fn=lambda: now[0] - NOW + 500.0)
        json.dump({"now": now[0], "aircraft": [
            {"hex": "a1b2c3", "flight": "UAL1 ", "lat": 42.5, "lon": -71.4, "alt_baro": 9000, "gs": 250.2, "track": 270.1},
            {"hex": "d4e5f6", "lat": 41.9, "lon": -70.9, "alt_baro": "ground"},
            {"hex": "112233"}]}, open(aj, "w"))
        lg.poll_once()
        lg.flush_all()
        r = V.analyze([f"{d}/data/adsb_aircraft.csv"], now=NOW + 100)
        text = V.report(r)
        self.assertEqual(sorted(r["uploadable"]), ["A1B2C3", "D4E5F6"])
        self.assertIn("VERDICT: OK", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
