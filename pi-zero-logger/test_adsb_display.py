#!/usr/bin/env python3
"""Tests for adsb_display.py and adsb_display_setup.sh on synthetic data.
Run:  python3 test_adsb_display.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adsb_display as D  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.aj = f"{self.d}/aircraft.json"
        self.sj = f"{self.d}/status.json"
        self._d, self._ip, self._thr = D.dongle_present, D.get_ip, D.throttled
        D.dongle_present = lambda: True
        D.get_ip = lambda: "192.168.1.50"
        D.throttled = lambda: False

    def tearDown(self):
        D.dongle_present, D.get_ip, D.throttled = self._d, self._ip, self._thr
        self.tmp.cleanup()

    def write_aircraft(self, now, messages=1000, n=11, pos=10, rssi=-18.2):
        ac = []
        for i in range(n):
            a = {"hex": f"a{i:05x}", "rssi": rssi - i}
            if i < pos:
                a.update(lat=42.0 + i / 10, lon=-71.0)
            ac.append(a)
        json.dump({"now": now, "messages": messages, "aircraft": ac}, open(self.aj, "w"))

    def write_status(self, now, **kw):
        s = {"updated": now, "all_time_unique": 1234, "today_unique": 142,
             "last_new_icao": "A1B2C3", "last_new_ts": now - 120, "clock": "ok"}
        s.update(kw)
        json.dump(s, open(self.sj, "w"))

    def plain(self, now, cols=40, rows=15, col=None):
        col = col or D.Collector(self.aj, self.sj, self.d, time_fn=lambda: now)
        lines = D.render_lines(col.collect(), cols, rows)
        return [D.strip_ansi(l) for l in lines]


class TestRender(Base):
    def test_full_frame_content_and_width(self):
        now = 1_800_000_000.0
        self.write_aircraft(now)
        self.write_status(now)
        lines = self.plain(now)
        text = "\n".join(lines)
        for want in ("NOW  11 planes  10 w/pos", "TODAY    142 unique", "ALL-TIME 1234 unique",
                     "DONGLE ok DEC ok LOG ok", "NEW  A1B2C3  2m ago", "SIG  best -18.2",
                     "CLOCK ok  PWR ok", "IP 192.168.1.50"):
            self.assertIn(want, text)
        self.assertTrue(all(len(l) <= 40 for l in lines), lines)
        self.assertLessEqual(len(lines), 15)

    def test_decoder_down_still_renders(self):
        now = 1_800_000_000.0
        self.write_aircraft(now - 300)  # stale file
        self.write_status(now)
        text = "\n".join(self.plain(now))
        self.assertIn("decoder not running", text)
        self.assertIn("DEC !!", text)
        self.assertIn("TODAY    142 unique", text)

    def test_nothing_exists_at_all(self):
        lines = self.plain(1_800_000_000.0)
        text = "\n".join(lines)
        self.assertIn("decoder not running", text)
        self.assertIn("TODAY    -- unique", text)
        self.assertIn("LOG !!", text)

    def test_unsynced_clock_and_low_power_flagged(self):
        now = 1_800_000_000.0
        self.write_aircraft(now)
        self.write_status(now, clock="unsynced")
        D.throttled = lambda: True
        text = "\n".join(self.plain(now))
        self.assertIn("CLOCK UNSYNCED", text)
        self.assertIn("PWR LOW", text)

    def test_small_console_drops_low_priority_lines(self):
        now = 1_800_000_000.0
        self.write_aircraft(now)
        self.write_status(now)
        lines = self.plain(now, cols=20, rows=6)
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(len(l) <= 20 for l in lines), lines)
        text = "\n".join(lines)
        self.assertIn("NOW", text)
        self.assertIn("TODAY", text)
        self.assertNotIn("IP 192", text)

    def test_tall_console_shows_everything_without_scrolling_codes(self):
        now = 1_800_000_000.0
        self.write_aircraft(now)
        self.write_status(now)
        col = D.Collector(self.aj, self.sj, self.d, time_fn=lambda: now)
        lines = D.render_lines(col.collect(), 40, 30)
        fr = D.frame(lines, 30)
        self.assertNotIn("\n", fr)            # cursor-addressed, never scrolls
        self.assertIn("\033[1;1H", fr)
        self.assertTrue(fr.endswith("\033[J"))

    def test_message_rate_from_counter_delta(self):
        t = {"v": 1_800_000_000.0}
        col = D.Collector(self.aj, self.sj, self.d, time_fn=lambda: t["v"])
        self.write_aircraft(t["v"], messages=1000)
        col.collect()
        t["v"] += 10
        self.write_aircraft(t["v"], messages=1300)
        st = col.collect()
        self.assertAlmostEqual(st["rate"], 30.0, places=3)
        t["v"] += 10
        self.write_aircraft(t["v"], messages=50)     # decoder restarted
        self.assertIsNone(col.collect()["rate"])

    def test_bar_widths(self):
        segs = D.bar_segs(75, 150, 20)
        self.assertEqual(sum(len(s[0]) for s in segs), 20)
        self.assertEqual(len(segs[0][0]), 10)
        self.assertEqual(sum(len(s[0]) for s in D.bar_segs(None, 150, 20)), 20)
        self.assertEqual(D.bar_segs(10, 150, 0), [])

    def test_ago(self):
        self.assertEqual(D.ago(30), "30s")
        self.assertEqual(D.ago(120), "2m")
        self.assertEqual(D.ago(7200), "2h")
        self.assertEqual(D.ago(None), "?")


class TestPagesAndOverlay(Base):
    NOW = 1_800_000_000.0

    def st(self):
        self.write_aircraft(self.NOW)
        self.write_status(self.NOW)
        return D.Collector(self.aj, self.sj, self.d, time_fn=lambda: self.NOW).collect()

    def ui(self, **kw):
        u = {"page": 0, "msg": "", "hold": None, "wifi": None, "bias": None}
        u.update(kw)
        return u

    def plain_ui(self, ui, cols=40, rows=15, guide="off"):
        return [D.strip_ansi(l) for l in D.render_lines(self.st(), cols, rows, ui, guide)]

    def test_page1_keeps_stats_and_adds_legend_on_last_row(self):
        L = self.plain_ui(self.ui())
        self.assertEqual(len(L), 15)
        self.assertIn("NOW  11 planes", L[1])
        self.assertIn("ALL-TIME 1234 unique", L[3])
        self.assertIn("1:page", L[-1])
        self.assertTrue(all(len(x) <= 40 for x in L))

    def test_page2_shows_wifi_bias_clock(self):
        L = self.plain_ui(self.ui(page=1, wifi="HomeWifi 77%", bias="OFF"))
        text = "\n".join(L)
        self.assertIn("PAGE 2/2", text)
        self.assertIn("WIFI HomeWifi 77%", text)
        self.assertIn("BIAS TEE OFF", text)
        self.assertIn("CLOCK ok", text)
        self.assertIn("192.168.1.50", text)
        self.assertIn("1:page", L[-1])

    def test_page2_unknown_wifi_and_bias_still_render(self):
        text = "\n".join(self.plain_ui(self.ui(page=1)))
        self.assertIn("WIFI unknown", text)
        self.assertIn("BIAS TEE unknown", text)

    def test_message_replaces_legend_and_wraps(self):
        msg = "SHUTTING DOWN. Wait for the green LED to stop, then unplug."
        L = self.plain_ui(self.ui(msg=msg))
        self.assertNotIn("1:page", "\n".join(L))
        self.assertEqual(" ".join(x.strip() for x in L[-2:]).replace("  ", " ")[:20], "SHUTTING DOWN. Wait ")
        self.assertEqual(len(L), 15)
        self.assertTrue(all(len(x) <= 40 for x in L))

    def test_hold_bar_shows_progress(self):
        L = self.plain_ui(self.ui(hold={"label": "B2 BIAS TEE", "progress": 0.5}))
        self.assertIn("B2 BIAS TEE [", L[-1])
        self.assertTrue(L[-1].rstrip().endswith("]"))
        raw = D.render_lines(self.st(), 40, 15, self.ui(hold={"label": "B2 BIAS TEE", "progress": 0.5}), "off")[-1]
        self.assertIn("43m", raw)      # yellow filled part
        self.assertIn("40;37m", raw)   # empty part

    def test_small_console_keeps_bottom_area(self):
        L = self.plain_ui(self.ui(hold={"label": "B3 SHUTDOWN", "progress": 0.2}), cols=30, rows=6)
        self.assertEqual(len(L), 6)
        self.assertIn("B3 SHUTDOWN", L[-1])
        self.assertTrue(all(len(x) <= 30 for x in L))

    def test_guide_top_has_four_cells_with_tap_hold_row(self):
        L = self.plain_ui(self.ui(bias="OFF"), guide="top")
        self.assertEqual(len(L), 15)
        self.assertEqual(len(L[0]), 40)
        self.assertEqual(L[0].split(), ["PAGE", "1/2", "BIAS", "ON", "POWER", "OFF", "SD", "COPY"])
        self.assertEqual(L[1].split(), ["tap", "hold", "3s", "hold", "3s", "tap"])
        # each label sits inside its own quarter of the row (cells are 10 wide)
        for i, word in enumerate(["PAGE 1/2", "BIAS ON", "POWER OFF", "SD COPY"]):
            self.assertIn(word, L[0][i * 10:(i + 1) * 10])
        self.assertIn("tap", L[1][0:10])
        self.assertIn("hold 3s", L[1][10:20])
        self.assertIn("hold 3s", L[1][20:30])
        self.assertIn("tap", L[1][30:40])
        self.assertIn("NOW  11 planes", "\n".join(L))
        self.assertNotIn("1:page", "\n".join(L))

    def test_guide_bottom_sits_below_message_and_hold(self):
        L = self.plain_ui(self.ui(msg="BIAS TEE ON"), guide="bottom")
        self.assertIn("PAGE 1/2", L[-2])
        self.assertIn("BIAS TEE ON", L[-3])
        L = self.plain_ui(self.ui(hold={"label": "B3 POWER OFF", "progress": 0.3}), guide="bottom")
        self.assertIn("B3 POWER OFF [", L[-3])
        self.assertIn("tap", L[-1])

    def test_guide_follows_page_and_bias_state_and_colours(self):
        L = self.plain_ui(self.ui(page=1, bias="ON"), guide="top")
        self.assertIn("PAGE 2/2", L[0])
        self.assertIn("BIAS OFF", L[0])      # label = what the button does next
        self.assertNotIn("BIAS ON", L[0])
        self.assertIn("BIAS ?", self.plain_ui(self.ui(), guide="top")[0])
        raw = D.render_lines(self.st(), 40, 15, self.ui(bias="ON"), "top")[0]
        self.assertIn("30;43m", raw)             # bias cell turns yellow when ON
        raw = D.render_lines(self.st(), 40, 15, self.ui(bias="OFF"), "top")[0]
        self.assertNotIn("30;43m", raw)

    def test_guide_small_console_single_row_and_narrow_cols(self):
        L = self.plain_ui(self.ui(bias="OFF"), cols=40, rows=10, guide="top")
        self.assertEqual(len(L), 10)
        self.assertIn("PAGE 1/2", L[0])
        self.assertNotIn("tap", "\n".join(L))
        L = self.plain_ui(self.ui(bias="OFF"), cols=24, rows=15, guide="top")
        self.assertTrue(all(len(x) <= 24 for x in L))
        self.assertEqual(len(L[0]), 24)

    def test_guide_off_keeps_old_legend(self):
        self.assertIn("1:page", self.plain_ui(self.ui(), guide="off")[-1])

    def test_cli_guide_option_validates(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "adsb_display.py"), "--guide", "left", "--plain"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_load_ui_valid_stale_and_garbage(self):
        p = f"{self.d}/ui.json"
        now = 1_800_000_000.0
        json.dump({"page": 3, "msg": "hi", "msg_until": now + 5, "wifi": "X 50%", "bias": "ON",
                   "hold": {"label": "B2 BIAS TEE", "progress": 1.7}, "ts": now - 1}, open(p, "w"))
        u = D.load_ui(p, now)
        self.assertEqual((u["page"], u["msg"], u["wifi"], u["bias"]), (1, "hi", "X 50%", "ON"))
        self.assertEqual(u["hold"]["progress"], 1.0)
        # expired message and stale hold (daemon died mid-hold) are dropped
        u = D.load_ui(p, now + 10)
        self.assertEqual((u["msg"], u["hold"]), ("", None))
        for bad in ("not json", "[1,2]", '{"page": "x", "hold": 5, "msg": 3}'):
            open(p, "w").write(bad)
            u = D.load_ui(p, now)
            self.assertEqual((u["page"], u["msg"], u["hold"]), (0, "", None))
        self.assertEqual(D.load_ui(f"{self.d}/missing.json", now)["page"], 0)

    def test_service_loop_redraws_quickly_on_ui_change(self):
        now = time.time()
        self.write_aircraft(now)
        self.write_status(now)
        ui = f"{self.d}/ui.json"
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "adsb_display.py"), "--cols", "40", "--rows", "15",
                              "--interval", "30", "--aircraft-json", self.aj, "--status-json", self.sj,
                              "--data-dir", self.d, "--ui-json", ui], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(1.0)
        json.dump({"page": 1, "wifi": "HomeWifi 77%", "bias": "OFF", "ts": time.time()}, open(ui, "w"))
        time.sleep(1.0)        # interval is 30 s: only a fast UI redraw can show page 2
        p.terminate()
        out, err = p.communicate(timeout=5)
        text = D.strip_ansi(out.decode())
        self.assertIn("PAGE 2/2", text)
        self.assertIn("HomeWifi 77%", text)
        self.assertNotIn(b"Traceback", err)


class TestColors(unittest.TestCase):
    def test_each_label_sits_on_its_own_colour(self):
        lines = D.color_bars(40, 15)
        joined = "\n".join(lines)
        for name, bg in (("RED", "41"), ("GREEN", "42"), ("BLUE", "44"), ("YELLOW", "43"),
                         ("CYAN", "46"), ("MAGENTA", "45"), ("WHITE", "47"), ("BLACK", "40")):
            m = re.search(rf"\033\[{bg};\d+m([^\033]*)\033\[0m", joined)
            self.assertIsNotNone(m, name)
        # the label text is inside the cell with that background
        self.assertRegex(joined, r"\033\[41;37m\s*RED\s*\033\[0m")
        self.assertRegex(joined, r"\033\[44;37m\s*BLUE\s*\033\[0m")
        self.assertTrue(all(len(D.strip_ansi(l)) <= 40 for l in lines))


class TestCLI(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "adsb_display.py"), *args,
                               "--aircraft-json", self.aj, "--status-json", self.sj,
                               "--data-dir", self.d], capture_output=True, text=True, timeout=20)

    def test_plain_preview_runs(self):
        now = time.time()
        self.write_aircraft(now)
        self.write_status(now)
        r = self.run_cli("--plain", "--cols", "40", "--rows", "15")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TODAY    142 unique", r.stdout)
        self.assertNotIn("\033", r.stdout)

    def test_plain_colors_runs(self):
        r = self.run_cli("--colors", "--plain", "--cols", "40", "--rows", "15")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RED", r.stdout)
        self.assertIn("BLUE", r.stdout)

    def test_service_loop_draws_and_exits_on_sigterm(self):
        now = time.time()
        self.write_aircraft(now)
        self.write_status(now)
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "adsb_display.py"),
                              "--aircraft-json", self.aj, "--status-json", self.sj,
                              "--data-dir", self.d, "--interval", "0.3",
                              "--cols", "40", "--rows", "15"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.2)
        p.terminate()
        out, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("\033[9;0]", out)                 # screen blanking disabled
        self.assertIn("142", out)
        self.assertGreaterEqual(out.count("\033[1;1H"), 2)   # redrawn more than once


class TestSetupScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.t = t
        os.makedirs(f"{t}/bin")
        os.makedirs(f"{t}/prefix")
        os.makedirs(f"{t}/units")
        self.log = f"{t}/calls.log"
        with open(f"{t}/bin/systemctl", "w") as f:
            f.write(f'#!/usr/bin/env bash\necho "systemctl $*" >> "{self.log}"\n'
                    '[[ "$1" == "is-active" ]] && echo active\nexit 0\n')
        with open(f"{t}/bin/id", "w") as f:
            f.write('#!/usr/bin/env bash\n[[ "$1" == "adsblog" ]] && exit 0\nexec /usr/bin/id "$@"\n')
        for n in ("systemctl", "id"):
            os.chmod(f"{t}/bin/{n}", 0o755)
        self.env = dict(os.environ, PATH=f"{t}/bin:{os.environ['PATH']}",
                        PREFIX=f"{t}/prefix", UNIT_DIR=f"{t}/units", DISPLAY_ENV=f"{t}/display.env")
        self.script = os.path.join(HERE, "adsb_display_setup.sh")

    def tearDown(self):
        self.tmp.cleanup()

    def run_s(self, *a):
        return subprocess.run(["bash", self.script, *a], capture_output=True, text=True,
                              env=self.env, timeout=30)

    def calls(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def test_install_and_undo(self):
        r = self.run_s("install")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(os.access(f"{self.t}/prefix/adsb_display.py", os.X_OK))
        unit = open(f"{self.t}/units/adsb-display.service").read()
        self.assertIn(f"ExecStart=/usr/bin/python3 {self.t}/prefix/adsb_display.py", unit)
        self.assertIn("User=adsblog", unit)
        self.assertIn("TTYPath=/dev/tty1", unit)
        c = self.calls()
        self.assertIn("systemctl mask getty@tty1.service", c)
        self.assertIn("systemctl enable adsb-display.service", c)
        self.assertIn("systemctl restart adsb-display.service", c)
        # idempotent
        self.assertEqual(self.run_s("install").returncode, 0)
        # undo
        r = self.run_s("undo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(f"{self.t}/units/adsb-display.service"))
        self.assertIn("systemctl unmask getty@tty1.service", self.calls())
        self.assertIn("systemctl start getty@tty1.service", self.calls())

    def test_unit_uses_guide_variable_and_guide_command_switches_it(self):
        r = self.run_s("install")
        self.assertEqual(r.returncode, 0, r.stderr)
        unit = open(f"{self.t}/units/adsb-display.service").read()
        self.assertIn("Environment=GUIDE=top", unit)
        self.assertIn(f"EnvironmentFile=-{self.t}/display.env", unit)
        self.assertIn("adsb_display.py --guide ${GUIDE}", unit)
        r = self.run_s("guide", "bottom")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(f"{self.t}/display.env").read().strip(), "GUIDE=bottom")
        self.assertIn("systemctl restart adsb-display.service", self.calls())
        self.assertIn("bottom", r.stdout)
        r = self.run_s("guide", "sideways")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(open(f"{self.t}/display.env").read().strip(), "GUIDE=bottom")

    def test_guide_requires_install(self):
        r = self.run_s("guide", "top")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("install first", r.stderr)

    def test_colors_mode_stops_and_restarts_service(self):
        self.assertEqual(self.run_s("install").returncode, 0)
        open(self.log, "w").close()
        # send the colour bars to a file instead of the real console
        with open(f"{self.t}/bin/python3", "w") as f:
            f.write(f'#!/usr/bin/env bash\necho "python3 $*" >> "{self.log}"\n')
        os.chmod(f"{self.t}/bin/python3", 0o755)
        r = self.run_s("colors")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls().splitlines()
        self.assertIn("systemctl stop adsb-display.service", c)
        self.assertTrue(any("--colors --hold 20" in l for l in c))
        self.assertEqual(c[-1], "systemctl start adsb-display.service")

    def test_missing_python_file_fails_cleanly(self):
        import shutil
        d2 = tempfile.mkdtemp()
        shutil.copy(self.script, d2)
        r = subprocess.run(["bash", f"{d2}/adsb_display_setup.sh", "install"],
                           capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("adsb_display.py must be in the same folder", r.stderr)
        self.assertFalse(os.path.exists(f"{self.t}/units/adsb-display.service"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
