#!/usr/bin/env python3
"""Tests for adsb_buttons.py and adsb_buttons_setup.sh with a fake clock, fake GPIO levels and a
recording command runner (nothing touches real hardware or systemd).
Run: python3 test_adsb_buttons.py"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adsb_buttons as B  # noqa: E402
import adsb_display as D  # noqa: E402

DT = 0.02


class Rig:
    """Drives Buttons.step() on a fake clock. down = set of button numbers currently pressed."""

    def __init__(self, tmp, bias_script=True, export_script=True, state="off"):
        self.t = 1000.0
        self.down = set()
        self.calls = []
        self.tmp = tmp
        self.ui_path = f"{tmp}/run/ui.json"
        self.state_file = f"{tmp}/biast"
        if state:
            open(self.state_file, "w").write(state + "\n")
        self.bias_script = f"{tmp}/biast.sh"
        self.export_script = f"{tmp}/export.py"
        if bias_script:
            open(self.bias_script, "w").write("")
        if export_script:
            open(self.export_script, "w").write("")
        self.rc = {}
        self.nmcli = "yes:HomeWifi:77\nno:Other:40\n"
        self.b = B.Buttons(self.levels, lambda: self.t, self.ui_path, self.runner,
                           self.state_file, self.bias_script, self.export_script)

    def levels(self):
        return {B.PINS[b]: (0 if b in self.down else 1) for b in B.PINS}

    def runner(self, cmd, timeout):
        self.calls.append(cmd)
        if cmd[0] == "nmcli":
            return 0, self.nmcli
        if cmd[:2] == ["bash", self.bias_script]:
            if self.rc.get("bias", 0) == 0:
                open(self.state_file, "w").write(cmd[2] + "\n")
            return self.rc.get("bias", 0), "out"
        return self.rc.get(cmd[0], 0), ""

    def run(self, seconds, down=None):
        if down is not None:
            self.down = set(down)
        for _ in range(int(round(seconds / DT))):
            self.t += DT
            self.b.step()

    def press(self, buttons, seconds):
        self.run(seconds, buttons)
        self.run(0.2, ())

    def ui(self):
        return json.load(open(self.ui_path))

    def cmds(self):
        return [c for c in self.calls if c[0] != "nmcli"]


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Rig(self.tmp.name)
        self.r.run(0.5)        # arm all buttons (seen released)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gpio_register_decode(self):
        p = f"{self.tmp.name}/gpiomem"
        buf = bytearray(4096)
        word = (1 << 16) | (1 << 21) | (1 << 5)          # 16 and 21 high, 20 and 26 low
        struct.pack_into("<I", buf, B.GPLEV0, word)
        open(p, "wb").write(buf)
        g = B.Gpio(p)
        self.assertEqual(g.levels(), {16: 1, 20: 0, 21: 1, 26: 0})
        g.close()

    def test_button1_cycles_pages(self):
        self.assertEqual(self.r.ui()["page"], 0)
        self.r.press({1}, 0.2)
        self.assertEqual(self.r.ui()["page"], 1)
        self.r.press({1}, 0.2)
        self.assertEqual(self.r.ui()["page"], 0)
        self.assertEqual(self.r.cmds(), [])

    def test_button4_exports_now(self):
        self.r.press({4}, 0.2)
        self.assertIn(["python3", self.r.export_script, "--once"], self.r.cmds())
        self.assertIn("SD export done", self.r.b.log)

    def test_button4_when_export_not_installed(self):
        r = Rig(self.tmp.name + "/x" if os.makedirs(self.tmp.name + "/x", exist_ok=True) is None else "", export_script=False)
        r.run(0.5)
        r.press({4}, 0.2)
        self.assertEqual(r.cmds(), [])
        self.assertIn("SD export not installed", r.b.log[-1])

    def test_button2_hold_toggles_bias_on_then_off(self):
        self.r.run(1.5, {2})
        h = self.r.ui()["hold"]
        self.assertEqual(h["label"], "B2 BIAS TEE")
        self.assertTrue(0.4 <= h["progress"] <= 0.6, h)
        self.assertEqual(self.r.cmds(), [])                      # nothing before the 3 s
        self.r.run(2.0)
        self.assertEqual(self.r.cmds(), [["bash", self.r.bias_script, "on"]])
        self.assertIsNone(self.r.ui()["hold"])
        self.assertEqual(self.r.ui()["bias"], "ON")
        self.assertIn("BIAS TEE ON", self.r.b.log[-1])
        self.assertIn("preamp must be connected", self.r.b.log[-1])
        self.r.run(0.3, ())                                       # release: must not fire twice
        self.assertEqual(len(self.r.cmds()), 1)
        self.r.press({2}, 3.3)
        self.assertEqual(self.r.cmds()[-1], ["bash", self.r.bias_script, "off"])
        self.assertEqual(self.r.ui()["bias"], "OFF")
        self.assertEqual(self.r.b.log[-1], "BIAS TEE OFF")

    def test_button2_tap_does_nothing_but_hint(self):
        self.r.press({2}, 1.0)
        self.assertEqual(self.r.cmds(), [])
        self.assertIn("cancelled", self.r.b.log[-1])
        self.assertIsNone(self.r.ui()["hold"])

    def test_button2_failure_reported(self):
        self.r.rc["bias"] = 1
        self.r.press({2}, 3.2)
        self.assertIn("FAILED", self.r.b.log[-1])
        self.assertEqual(self.r.ui()["bias"], "OFF")

    def test_button2_without_bias_script(self):
        r = Rig(self.tmp.name + "/y" if os.makedirs(self.tmp.name + "/y", exist_ok=True) is None else "", bias_script=False)
        r.run(0.5)
        r.press({2}, 3.2)
        self.assertEqual(r.cmds(), [])
        self.assertIn("not installed", r.b.log[-1])

    def test_button3_hold_exports_then_powers_off(self):
        self.r.run(2.5, {3})
        self.assertEqual(self.r.cmds(), [])
        self.r.run(1.0)
        c = self.r.cmds()
        self.assertEqual(c[0], ["python3", self.r.export_script, "--once"])
        self.assertEqual(c[-1], ["systemctl", "poweroff"])
        self.assertIn("SHUTTING DOWN", self.r.ui()["msg"])
        self.assertGreater(self.r.ui()["msg_until"], self.r.t + 500)

    def test_shutdown_tap_does_not_shut_down(self):
        self.r.press({3}, 2.9)
        self.assertEqual(self.r.cmds(), [])

    def test_button_held_at_startup_is_ignored_until_released(self):
        r = Rig(self.tmp.name + "/z" if os.makedirs(self.tmp.name + "/z", exist_ok=True) is None else "")
        r.run(10.0, {3})                       # held down since power on
        self.assertEqual(r.cmds(), [])
        r.run(0.3, ())                          # released: now armed
        r.press({3}, 3.2)
        self.assertEqual(r.cmds()[-1], ["systemctl", "poweroff"])

    def test_all_buttons_stuck_low_never_act(self):
        r = Rig(self.tmp.name + "/w" if os.makedirs(self.tmp.name + "/w", exist_ok=True) is None else "")
        r.run(30.0, {1, 2, 3, 4})
        self.assertEqual(r.cmds(), [])
        self.assertEqual(r.b.log, [])

    def test_two_buttons_together_ignored(self):
        self.r.run(4.0, {2, 3})
        self.assertEqual(self.r.cmds(), [])
        self.assertIn("several buttons", self.r.b.log[-1])

    def test_short_glitch_is_debounced(self):
        self.r.run(0.02, {1})
        self.r.run(0.5, ())
        self.assertEqual(self.r.ui()["page"], 0)

    def test_wifi_and_bias_info_on_screen_state(self):
        self.r.run(11.0)
        u = self.r.ui()
        self.assertEqual(u["wifi"], "HomeWifi 77%")
        self.assertEqual(u["bias"], "OFF")
        self.r.nmcli = "no:Other:40\n"
        self.r.run(11.0)
        self.assertEqual(self.r.ui()["wifi"], "not connected")
        self.r.nmcli = "yes:Home\\:Wifi:60\n"
        self.r.run(11.0)
        self.assertEqual(self.r.ui()["wifi"], "Home:Wifi 60%")

    def test_ui_json_is_world_readable_and_atomic(self):
        self.assertEqual(oct(os.stat(self.r.ui_path).st_mode & 0o777), "0o644")
        self.assertFalse(os.path.exists(self.r.ui_path + ".tmp"))

    def test_message_expires(self):
        self.r.press({1}, 0.2)
        self.r.b.say("hello", 2)
        self.r.run(3.0)
        self.assertEqual(self.r.ui()["msg"], "")

    def test_end_to_end_with_the_real_display_code(self):
        """What the daemon writes is what the screen shows: mid-hold bar, then message."""
        self.r.run(1.5, {2})
        ui = D.load_ui(self.r.ui_path, self.r.t)
        st = {"now": self.r.t, "host": "adsb-zero", "decoder_ok": True, "planes": 3, "with_pos": 2, "today": 5,
              "alltime": 9, "dongle": True, "logger_ok": True, "last_icao": None, "last_ts": None, "rate": 30.0,
              "best_rssi": -20.0, "clock": "ok", "throttled": False, "ip": "1.2.3.4", "free": 50.0, "temp": 40.0}
        last = D.strip_ansi(D.render_lines(st, 40, 15, ui)[-1])
        self.assertIn("B2 BIAS TEE [", last)
        self.r.run(2.0)
        ui = D.load_ui(self.r.ui_path, self.r.t)
        self.assertIn("BIAS TEE ON", "\n".join(D.strip_ansi(l) for l in D.render_lines(st, 40, 15, ui)[-2:]))
        self.r.press({1}, 0.2)
        ui = D.load_ui(self.r.ui_path, self.r.t)
        page2 = "\n".join(D.strip_ansi(l) for l in D.render_lines(st, 40, 15, ui))
        self.assertIn("BIAS TEE ON", page2)
        self.assertIn("WIFI HomeWifi 77%", page2)


class TestSelftestAndCLI(unittest.TestCase):
    def test_selftest_counts_presses(self):
        class G:
            def __init__(s):
                s.i = 0
                s.seq = [{16: 1, 20: 1, 21: 1, 26: 1}] * 3 + [{16: 0, 20: 1, 21: 1, 26: 1}] * 2 + \
                        [{16: 1, 20: 0, 21: 1, 26: 1}] * 2 + [{16: 1, 20: 1, 21: 0, 26: 1}] * 2 + \
                        [{16: 1, 20: 1, 21: 1, 26: 0}] * 2 + [{16: 1, 20: 1, 21: 1, 26: 1}] * 50

            def levels(s):
                v = s.seq[min(s.i, len(s.seq) - 1)]
                s.i += 1
                return v
        out = []
        tt = [0.0]
        rc = B.selftest(1, G(), out.append, clock=lambda: tt.__setitem__(0, tt[0] + 0.01) or tt[0], sleep=lambda s: None)
        text = "\n".join(out)
        self.assertEqual(rc, 0, text)
        self.assertIn("Button 1 (GPIO16) PRESSED", text)
        self.assertIn("Button 4 (GPIO26) PRESSED", text)
        self.assertIn("All four buttons work.", text)

    def test_selftest_reports_missing_and_stuck(self):
        class G:
            def levels(s):
                return {16: 0, 20: 1, 21: 1, 26: 1}
        out = []
        tt = [0.0]
        rc = B.selftest(1, G(), out.append, clock=lambda: tt.__setitem__(0, tt[0] + 0.1) or tt[0], sleep=lambda s: None)
        text = "\n".join(out)
        self.assertEqual(rc, 2)
        self.assertIn("reads LOW at rest", text)
        self.assertIn("No press seen on: B1, B2, B3, B4", text)

    def test_cli_test_mode_and_missing_gpiomem(self):
        d = tempfile.mkdtemp()
        buf = bytearray(4096)
        struct.pack_into("<I", buf, B.GPLEV0, sum(1 << p for p in B.PINS.values()))
        open(f"{d}/g", "wb").write(buf)
        r = subprocess.run([sys.executable, os.path.join(HERE, "adsb_buttons.py"), "--test", "1", "--gpiomem", f"{d}/g"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("B1(GPIO16)=high", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(HERE, "adsb_buttons.py"), "--gpiomem", f"{d}/nope"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot open", r.stderr)


class TestSetupScript(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        for d in ("pfx", "units", "home"):
            os.makedirs(f"{self.t}/{d}")
        buf = bytearray(4096)
        struct.pack_into("<I", buf, B.GPLEV0, sum(1 << p for p in B.PINS.values()))
        open(f"{self.t}/g", "wb").write(buf)
        self.env = dict(os.environ, PREFIX=f"{self.t}/pfx", UNIT_DIR=f"{self.t}/units", NO_SYSTEMCTL="1",
                        HOME_OVERRIDE=f"{self.t}/home", GPIOMEM=f"{self.t}/g")
        self.sh = os.path.join(HERE, "adsb_buttons_setup.sh")

    def sh_run(self, *a):
        return subprocess.run(["bash", self.sh, *a], capture_output=True, text=True, env=self.env)

    def test_install_status_test_undo(self):
        r = self.sh_run("install")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("Button 2 (bias tee):  not available yet", r.stdout)
        unit = open(f"{self.t}/units/adsb-buttons.service").read()
        self.assertIn("User=root", unit)
        self.assertIn("RuntimeDirectory=adsb-buttons", unit)
        self.assertIn(f"ExecStart=/usr/bin/python3 {self.t}/pfx/adsb_buttons.py", unit)
        self.assertTrue(os.path.exists(f"{self.t}/pfx/adsb_display.py"))
        open(f"{self.t}/pfx/adsb_biast_setup.sh", "w").write("")
        self.assertIn("Button 2 (bias tee):  ready", self.sh_run("install").stdout)
        r = self.sh_run("status")
        self.assertIn("gpiomem         : present", r.stdout)
        self.assertTrue(os.path.exists(f"{self.t}/home/buttons_status.txt"))
        r = self.sh_run("test")
        self.assertIn("B2(GPIO20)=high", r.stdout)
        self.assertTrue(os.path.exists(f"{self.t}/home/buttons_test.txt"))
        r = self.sh_run("undo")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(f"{self.t}/units/adsb-buttons.service"))

    def test_install_needs_both_files(self):
        import shutil
        d = tempfile.mkdtemp()
        shutil.copy(self.sh, d)
        shutil.copy(os.path.join(HERE, "adsb_buttons.py"), d)
        r = subprocess.run(["bash", f"{d}/adsb_buttons_setup.sh", "install"], capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("adsb_display.py", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
