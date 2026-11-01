#!/usr/bin/env python3
"""
adsb_buttons.py - the four Argon POD display buttons for the ADS-B Zero.

From the POD manual (wiring diagram): each button pulls its GPIO to ground, with an external 10k
pull-up to 3V3 and a 100 nF debounce capacitor. So idle reads HIGH and pressed reads LOW.

    Button 1  GPIO16  short press   switch screen page (stats / network)
    Button 2  GPIO20  HOLD 3 s      toggle the RTL-SDR V4 bias tee (needs adsb_biast_setup.sh installed)
    Button 3  GPIO21  HOLD 3 s      safe shutdown (copies logs to the SD card first)
    Button 4  GPIO26  short press   copy logs to the SD card boot partition now

Reads the pin levels straight from the GPIO registers through /dev/gpiomem (read only, no extra
libraries, does not claim the pins). Talks to the screen through /run/adsb-buttons/ui.json.
Safety: a button that is already held down when this starts (or reads LOW forever) is ignored until
it has been seen released, and a hold only counts if no other button is down.

  adsb_buttons.py            run forever (the systemd service does this, as root)
  adsb_buttons.py --test [S] show live button events for S seconds (default 20); does nothing else
"""
import argparse
import json
import mmap
import os
import re
import signal
import struct
import subprocess
import sys
import time

PINS = {1: 16, 2: 20, 3: 21, 4: 26}
GPLEV0 = 0x34                      # GPIO pin level register (BCM2835/2837/2711, Pi Zero 2 W = BCM2710)
GPIOMEM = "/dev/gpiomem"
UI_JSON = "/run/adsb-buttons/ui.json"
BIAS_STATE = "/etc/adsb-zero/biast"
BIAS_SCRIPT = "/opt/adsb-zero/adsb_biast_setup.sh"
EXPORT_SCRIPT = "/opt/adsb-zero/adsb_sdexport.py"
DEBOUNCE = 0.04
HOLD = 3.0
POLL = 0.02
INFO_EVERY = 10.0
HOLD_BUTTONS = {2, 3}
NPAGES = 2


class Gpio:
    """Read-only view of the GPIO level register."""

    def __init__(self, path=GPIOMEM):
        fd = os.open(path, os.O_RDONLY | os.O_SYNC)
        try:
            self.m = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ)
        finally:
            os.close(fd)

    def levels(self):
        word = struct.unpack_from("<I", self.m, GPLEV0)[0]
        return {p: (word >> p) & 1 for p in PINS.values()}

    def close(self):
        self.m.close()


def default_runner(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 127, str(e)


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


class Buttons:
    def __init__(self, read_levels, now_fn=time.time, ui_path=UI_JSON, runner=default_runner,
                 bias_state=BIAS_STATE, bias_script=BIAS_SCRIPT, export_script=EXPORT_SCRIPT):
        self.read_levels, self.now, self.ui_path, self.run = read_levels, now_fn, ui_path, runner
        self.bias_state, self.bias_script, self.export_script = bias_state, bias_script, export_script
        self.s = {b: {"armed": False, "low_since": None, "down": False, "down_t": None, "fired": False}
                  for b in PINS}
        self.ui = {"page": 0, "msg": "", "msg_until": 0.0, "hold": None, "wifi": "", "bias": ""}
        self.last_written = None
        self.last_write_t = 0.0
        self.last_info = -1e9
        self.log = []

    # ---------- screen state ----------
    def write_ui(self, force=False):
        t = self.now()
        body = dict(self.ui)
        sig = json.dumps(body, sort_keys=True)
        if not force and sig == self.last_written and t - self.last_write_t < 2.0:
            return
        body["ts"] = t
        d = os.path.dirname(self.ui_path)
        try:
            os.makedirs(d, exist_ok=True)
            tmp = self.ui_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(body, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.ui_path)
            self.last_written, self.last_write_t = sig, t
        except OSError as e:
            print("cannot write", self.ui_path, e, file=sys.stderr)

    def say(self, text, seconds=6.0):
        self.ui["msg"] = text
        self.ui["msg_until"] = self.now() + seconds
        self.log.append(text)
        print(text, flush=True)
        self.write_ui(force=True)

    def refresh_info(self):
        """WiFi name/signal and requested bias tee state for page 2."""
        self.last_info = self.now()
        rc, out = self.run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi"], 5)
        wifi = "not connected"
        if rc == 0:
            for line in out.splitlines():
                m = re.match(r"^yes:(.*):(\d+)$", line)
                if m:
                    wifi = "%s %s%%" % (m.group(1).replace("\\:", ":"), m.group(2))
                    break
        self.ui["wifi"] = wifi
        v = read_text(self.bias_state)
        self.ui["bias"] = "ON" if v == "on" else "OFF"
        self.write_ui()

    # ---------- actions ----------
    def act_page(self):
        self.ui["page"] = (self.ui["page"] + 1) % NPAGES
        self.write_ui(force=True)

    def act_bias(self):
        if not os.path.exists(self.bias_script):
            return self.say("BIAS: not installed. Run adsb_biast_setup.sh install", 8)
        cur = read_text(self.bias_state)
        new = "off" if cur == "on" else "on"
        self.say("BIAS TEE -> %s ... decoder restarting" % new.upper(), 120)
        rc, out = self.run(["bash", self.bias_script, new], 120)
        self.refresh_info()
        if rc == 0:
            self.say("BIAS TEE %s%s" % (new.upper(), "  (preamp must be connected)" if new == "on" else ""), 8)
        else:
            self.say("BIAS TEE change FAILED (see: journalctl -u adsb-buttons)", 10)
            print(out[-400:], file=sys.stderr, flush=True)

    def act_export(self, quiet=False):
        if not os.path.exists(self.export_script):
            if not quiet:
                self.say("SD export not installed (adsb_sdexport_setup.sh)", 6)
            return False
        if not quiet:
            self.say("Copying logs to the SD card ...", 60)
        rc, out = self.run(["python3", self.export_script, "--once"], 60)
        if not quiet:
            self.say("SD export done" if rc == 0 else "SD export FAILED", 6)
        return rc == 0

    def act_shutdown(self):
        self.say("SHUTTING DOWN. Wait for the green LED to stop, then unplug.", 600)
        self.act_export(quiet=True)
        self.run(["systemctl", "poweroff"], 30)

    # ---------- state machine ----------
    def step(self):
        t = self.now()
        lv = self.read_levels()
        low = {b for b, p in PINS.items() if lv.get(p) == 0}
        for b in PINS:
            st = self.s[b]
            if b in low:
                if st["low_since"] is None:
                    st["low_since"] = t
                if not st["down"] and st["armed"] and t - st["low_since"] >= DEBOUNCE:
                    st["down"], st["down_t"], st["fired"] = True, st["low_since"], False
            else:
                released = st["down"]
                was_fired = st["fired"]
                dur = (t - st["down_t"]) if st["down"] else 0.0
                st["low_since"], st["down"], st["down_t"], st["fired"] = None, False, None, False
                st["armed"] = True
                if released and not was_fired:
                    if b in HOLD_BUTTONS:
                        self.ui["hold"] = None
                        self.say("cancelled (hold the button for %d s)" % HOLD, 2)
                    elif b == 1:
                        self.act_page()
                    elif b == 4:
                        self.act_export()
        held = [b for b in PINS if self.s[b]["down"]]
        hb = [b for b in held if b in HOLD_BUTTONS]
        if hb:
            b = hb[0]
            st = self.s[b]
            elapsed = t - st["down_t"]
            if len(held) > 1:
                if not st["fired"]:
                    st["fired"] = True
                    self.ui["hold"] = None
                    self.say("several buttons down: ignored", 3)
            elif not st["fired"]:
                label = "B%d %s" % (b, "BIAS TEE" if b == 2 else "SHUTDOWN")
                if elapsed >= HOLD:
                    st["fired"] = True
                    self.ui["hold"] = None
                    self.write_ui(force=True)
                    (self.act_bias if b == 2 else self.act_shutdown)()
                else:
                    self.ui["hold"] = {"label": label, "progress": round(elapsed / HOLD * 20) / 20}
                    self.write_ui()
        elif self.ui["hold"]:
            self.ui["hold"] = None
            self.write_ui(force=True)
        if t - self.last_info >= INFO_EVERY:
            self.refresh_info()
        elif self.ui["msg"] and t >= self.ui["msg_until"]:
            self.ui["msg"] = ""
            self.write_ui(force=True)
        else:
            self.write_ui()


def selftest(seconds, gpio, out=print, clock=time.time, sleep=time.sleep):
    out("Button test for %d seconds. Press each button a few times." % seconds)
    lv = gpio.levels()
    out("Idle levels now: " + "  ".join("B%d(GPIO%d)=%s" % (b, p, "high" if lv[p] else "LOW")
                                        for b, p in PINS.items()))
    if any(v == 0 for v in lv.values()):
        out("NOTE: a button reads LOW at rest. Is it being held, or is a wire stuck?")
    counts = {b: 0 for b in PINS}
    last = dict(lv)
    t_end = clock() + seconds
    while clock() < t_end:
        cur = gpio.levels()
        for b, p in PINS.items():
            if cur[p] != last[p]:
                if cur[p] == 0:
                    counts[b] += 1
                out("  Button %d (GPIO%d) %s" % (b, p, "PRESSED" if cur[p] == 0 else "released"))
                last[p] = cur[p]
        sleep(0.01)
    out("Summary: " + ", ".join("B%d pressed %d times" % (b, n) for b, n in counts.items()))
    bad = [b for b, n in counts.items() if n == 0]
    out("All four buttons work." if not bad else "No press seen on: " + ", ".join("B%d" % b for b in bad))
    return 0 if not bad else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", nargs="?", const=20, type=int)
    ap.add_argument("--gpiomem", default=GPIOMEM)
    ap.add_argument("--ui-json", default=UI_JSON)
    a = ap.parse_args(argv)
    try:
        gpio = Gpio(a.gpiomem)
    except OSError as e:
        print("cannot open %s: %s (run as root)" % (a.gpiomem, e), file=sys.stderr)
        return 1
    if a.test is not None:
        return selftest(a.test, gpio)
    b = Buttons(gpio.levels, ui_path=a.ui_json)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    print("buttons ready: B1 page, B2 hold bias tee, B3 hold shutdown, B4 export", flush=True)
    b.refresh_info()
    b.write_ui(force=True)
    while not stop:
        try:
            b.step()
        except Exception as e:
            print("button loop error: %r" % (e,), file=sys.stderr, flush=True)
            time.sleep(1)
        time.sleep(POLL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
