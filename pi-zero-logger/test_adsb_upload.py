#!/usr/bin/env python3
"""Tests for adsb_upload.py against a fake WDGoWars server that enforces what the real one does:
HMAC signature, X-API-Key, a non-default User-Agent (else 403 like Cloudflare), per-user dedup.
Run: python3 test_adsb_upload.py"""
import base64, hashlib, hmac, io, json, os, subprocess, sys, tempfile, threading, unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adsb_upload as U  # noqa: E402

KEY = "k3yABCDEFGH1234567890zz"
NOW = 1_793_488_000
HDR = "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n"


class Fake(BaseHTTPRequestHandler):
    seen = set()
    log = []
    force = []      # status codes to return next

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ua = self.headers.get("User-Agent", "")
        if not ua or ua.startswith("python-requests") or ua.startswith("Python-urllib"):
            return self.reply(403, {"error": "error code: 1010"})
        if Fake.force:
            return self.reply(Fake.force.pop(0), {"error": "forced"})
        if self.headers.get("X-API-Key") != KEY:
            return self.reply(401, {"error": "bad key"})
        env = json.loads(body)
        exp = hmac.new(KEY.encode(), (env["nonce"] + env["data"]).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(exp, env["sig"]):
            return self.reply(400, {"error": "bad signature"})
        data = json.loads(base64.b64decode(env["data"]))
        Fake.log.append((ua, data))
        imp = dup = 0
        for ac in data.get("aircraft", []):
            if ac["icao"] in Fake.seen:
                dup += 1
            else:
                Fake.seen.add(ac["icao"]); imp += 1
        self.reply(200, {"imported": 0, "duplicates": 0, "aircraft_imported": imp,
                         "aircraft_already_seen": dup, "total": 42})


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.url = "http://127.0.0.1:%d/api/upload/" % cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        p = self.d.name
        self.csv, self.state, self.keyf = p + "/a.csv", p + "/sent.txt", p + "/key"
        open(self.keyf, "w").write(KEY + "\n")
        Fake.seen.clear(); Fake.log.clear(); Fake.force.clear()
        U.time.sleep = lambda s: None

    def tearDown(self):
        self.d.cleanup()

    def write(self, rows):
        open(self.csv, "w").write(HDR + "".join(r + "\n" for r in rows))

    def run_main(self, *extra):
        args = ["--csv", self.csv, "--state", self.state, "--key-file", self.keyf, "--url", self.url] + list(extra)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = U.main(args)
        return rc, buf.getvalue()

    def test_sign_matches_game_formula(self):
        raw = json.dumps({"a": 1}, separators=(",", ":")).encode()
        b64 = base64.b64encode(raw).decode()
        sig = hmac.new(KEY.encode(), ("abc123" + b64).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(json.loads(U.sign_payload({"a": 1}, KEY, "abc123")),
                         {"data": b64, "nonce": "abc123", "sig": sig})

    def test_parse_matches_game_rules_and_skips_bad_clock(self):
        self.write([f"{NOW},A1B2C3,UAL1,42.5,-71.4,9000,250,270,1200",
                    f"{NOW},ZERO00,,0,0,1,1,1,", f"{NOW},SHORT,x", f"{NOW},,,42.5,-71.4,1,1,1,",
                    "1000000,OLDCLK,,42.5,-71.4,1,1,1,", f"{NOW},A1B2C3,,43,-72,1,1,1,",
                    f"{NOW + 5},NOALT,,41.9,-70.9,,,,"])
        ac, sk = U.parse_csv(self.csv, now=NOW + 100)
        self.assertEqual([a["icao"] for a in ac], ["A1B2C3", "NOALT"])
        self.assertEqual(ac[0]["lat"], 42.5)
        self.assertEqual(ac[1]["alt_ft"], 0)
        self.assertEqual(ac[0]["type"], "ADSB")
        self.assertEqual(sk, {"no_valid_position": 1, "short_row": 1, "bad_clock": 1})

    def test_test_mode_ok_and_uses_allowed_user_agent(self):
        rc, out = self.run_main("--test")
        self.assertEqual(rc, 0, out)
        self.assertIn("TEST OK", out)
        self.assertTrue(Fake.log[0][0].startswith("WatchDogsGo/"))
        self.assertEqual(Fake.log[0][1], {"networks": []})

    def test_upload_sends_only_new_then_nothing(self):
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,9000,250,270,", f"{NOW},D4E5F6,,41.9,-70.9,100,1,1,"])
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("aircraft_imported=2", out)
        self.assertEqual(len(Fake.log[0][1]["aircraft"]), 2)
        self.assertEqual(Fake.log[0][1]["networks"], [])
        self.assertEqual(Fake.log[0][1]["meshcore_nodes"], [])
        # new aircraft appears later: only it is sent
        with open(self.csv, "a") as f:
            f.write(f"{NOW + 9},999999,,40.1,-70.1,1,1,1,\n")
        rc, out = self.run_main()
        self.assertEqual([a["icao"] for a in Fake.log[1][1]["aircraft"]], ["999999"])
        rc, out = self.run_main()
        self.assertIn("Nothing to upload", out)
        self.assertEqual(len(Fake.log), 2)

    def test_already_seen_counted_and_marked_sent(self):
        Fake.seen.add("A1B2C3")
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,", f"{NOW},D4E5F6,,41.9,-70.9,1,1,1,"])
        rc, out = self.run_main()
        self.assertIn("aircraft_imported=1  aircraft_already_seen=1", out)
        self.assertEqual(U.load_sent(self.state), {"A1B2C3", "D4E5F6"})

    def test_dry_run_sends_nothing(self):
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,"])
        rc, out = self.run_main("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(Fake.log, [])
        self.assertFalse(os.path.exists(self.state))

    def test_chunking(self):
        self.write([f"{NOW},{i:06X},,42.5,-71.4,1,1,1," for i in range(1, 8)])
        rc, out = self.run_main("--chunk", "3")
        self.assertEqual([len(l[1]["aircraft"]) for l in Fake.log], [3, 3, 1])
        self.assertIn("aircraft_imported=7", out)

    def test_wrong_key_fails_and_nothing_marked_sent(self):
        open(self.keyf, "w").write("wrongkeywrongkey12\n")
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,"])
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 401", out)
        self.assertNotIn("wrongkeywrongkey12", out)
        self.assertFalse(os.path.exists(self.state))

    def test_429_retries_once_then_succeeds(self):
        Fake.force.append(429)
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,"])
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("rate limited", out)
        self.assertIn("aircraft_imported=1", out)

    def test_server_error_stops_and_keeps_unsent_for_next_run(self):
        Fake.force.extend([500])
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,"])
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 500", out)
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("aircraft_imported=1", out)

    def test_connection_error_reported(self):
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,"])
        args = ["--csv", self.csv, "--state", self.state, "--key-file", self.keyf, "--url", "http://127.0.0.1:1/x"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = U.main(args)
        self.assertEqual(rc, 1)
        self.assertIn("connection error", buf.getvalue())

    def test_key_never_printed_and_missing_key_message(self):
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,"])
        rc, out = self.run_main()
        self.assertNotIn(KEY, out)
        os.remove(self.keyf)
        with self.assertRaises(SystemExit) as c:
            self.run_main("--test")
        self.assertIn("setkey", str(c.exception))

    def test_setup_script_setkey_test_upload_status(self):
        self.write([f"{NOW},A1B2C3,,42.5,-71.4,1,1,1,", f"{NOW},D4E5F6,,41.9,-70.9,1,1,1,"])
        os.remove(self.keyf)
        home = self.d.name + "/home"; os.makedirs(home)
        env = dict(os.environ, PREFIX=self.d.name + "/pfx", KEY_FILE=self.keyf, CSV=self.csv,
                   STATE=self.state, URL=self.url, HOME_OVERRIDE=home)
        sh = os.path.join(HERE, "adsb_upload_setup.sh")
        r = subprocess.run(["bash", sh, "setkey"], input=KEY + "\n", capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(KEY, r.stdout + r.stderr)
        self.assertEqual(oct(os.stat(self.keyf).st_mode & 0o777), "0o600")
        for mode, expect in (("test", "TEST OK"), ("dry", "DRY RUN"), ("upload", "aircraft_imported=2"),
                             ("status", "sent ICAOs recorded: 2")):
            r = subprocess.run(["bash", sh, mode], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, mode + r.stdout + r.stderr)
            self.assertIn(expect, r.stdout, mode)
            self.assertNotIn(KEY, r.stdout + r.stderr)
        self.assertIn("sent ICAOs", open(home + "/adsb_upload_result.txt").read())
        r = subprocess.run(["bash", sh, "setkey"], input="abc\n", capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("too short", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
