#!/usr/bin/env python3
"""
adsb_display.py - live ADS-B status for a small text console (the Argon POD screen).

Draws on the Linux console (tty1) with plain ANSI colours, so it works on any
framebuffer console. It adapts to the console size (40x15, 40x30, or smaller)
and drops the lowest-priority lines when there is little room.

Reads only files, no network, no root:
  /run/dump1090-fa/aircraft.json   planes now, message rate, signal
  /run/adsb-zero/status.json       unique counts and last new aircraft (from the logger)
  /run/adsb-buttons/ui.json        page, button messages and hold bar (from adsb_buttons.py, optional)
  /sys, /proc                      temperature, USB dongle, IP address, free space

Usage:
  adsb_display.py                run forever (the systemd service does this)
  adsb_display.py --plain        print ONE frame as plain text and exit (preview/testing)
  adsb_display.py --colors       draw 8 colour bars with labels (to check colours), then exit
                                 add --hold 20 to leave them on screen for 20 seconds
Options:  --cols N --rows N  override the detected console size.
          --guide top|bottom|off  where the button guide is drawn (default top).
"""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

AIRCRAFT_JSON = "/run/dump1090-fa/aircraft.json"
STATUS_JSON = "/run/adsb-zero/status.json"
UI_JSON = "/run/adsb-buttons/ui.json"     # written by adsb_buttons.py (page, messages, hold bar)
NPAGES = 2
DATA_DIR = "/var/lib/adsb-zero"

# ANSI SGR codes
RESET = "0"
BOLD_WHITE = "1;37"
CYAN = "1;36"
GREEN = "1;32"
RED = "1;31"
YELLOW = "1;33"
DIM = "37"

RTL_IDS = {("0bda", "2838"), ("0bda", "2832")}


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def dongle_present():
    base = "/sys/bus/usb/devices"
    try:
        for d in os.listdir(base):
            v = read_text(os.path.join(base, d, "idVendor"))
            p = read_text(os.path.join(base, d, "idProduct"))
            if v and p and (v.lower(), p.lower()) in RTL_IDS:
                return True
    except OSError:
        pass
    return False


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("0.") else ip
    except OSError:
        return None


def cpu_temp():
    t = read_text("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(t) / 1000.0
    except (TypeError, ValueError):
        return None


def throttled():
    """True = problem, False = ok, None = unknown."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        val = int(out.split("=")[1], 16)
        return val != 0
    except Exception:
        return None


def free_gb(path):
    try:
        st = os.statvfs(path if os.path.exists(path) else "/")
        return st.f_bavail * st.f_frsize / 1e9
    except OSError:
        return None


def ago(seconds):
    if seconds is None or seconds < 0:
        return "?"
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


class Collector:
    def __init__(self, aircraft_json=AIRCRAFT_JSON, status_json=STATUS_JSON,
                 data_dir=DATA_DIR, time_fn=time.time):
        self.aj, self.sj, self.dd, self.now = aircraft_json, status_json, data_dir, time_fn
        self.prev_msgs = None
        self.prev_t = None
        self.rate = None
        self.thr = None
        self.thr_t = 0.0

    def collect(self):
        now = self.now()
        st = {"now": now, "planes": 0, "with_pos": 0, "best_rssi": None}
        d = read_json(self.aj)
        if d and isinstance(d.get("now"), (int, float)) and now - d["now"] < 20:
            ac = d.get("aircraft", [])
            st["decoder_ok"] = True
            st["planes"] = len(ac)
            st["with_pos"] = sum(1 for a in ac if "lat" in a and "lon" in a)
            rs = [a["rssi"] for a in ac if isinstance(a.get("rssi"), (int, float))]
            st["best_rssi"] = max(rs) if rs else None
            m = d.get("messages")
            if isinstance(m, (int, float)):
                if self.prev_msgs is not None and d["now"] > self.prev_t and m >= self.prev_msgs:
                    self.rate = (m - self.prev_msgs) / (d["now"] - self.prev_t)
                elif self.prev_msgs is not None and m < self.prev_msgs:
                    self.rate = None  # decoder restarted
                self.prev_msgs, self.prev_t = m, d["now"]
        else:
            st["decoder_ok"] = False
            self.prev_msgs = self.prev_t = None
            self.rate = None
        st["rate"] = self.rate
        s = read_json(self.sj)
        if s and isinstance(s.get("updated"), (int, float)) and now - s["updated"] < 180:
            st["logger_ok"] = True
            st["today"] = s.get("today_unique")
            st["alltime"] = s.get("all_time_unique")
            st["last_icao"] = s.get("last_new_icao")
            st["last_ts"] = s.get("last_new_ts")
            st["clock"] = s.get("clock", "ok")
        else:
            st["logger_ok"] = False
            st["today"] = st["alltime"] = st["last_icao"] = st["last_ts"] = None
            st["clock"] = None
        st["dongle"] = dongle_present()
        st["ip"] = get_ip()
        st["temp"] = cpu_temp()
        st["free"] = free_gb(self.dd)
        if now - self.thr_t > 30:
            self.thr, self.thr_t = throttled(), now
        st["throttled"] = self.thr
        st["host"] = socket.gethostname()
        return st


# ---------- rendering ----------
# A line is a list of (text, sgr) segments.

def seg(text, sgr=DIM):
    return (str(text), sgr)


def ok_seg(label, good):
    return seg(label + (" ok" if good else " !!"), GREEN if good else RED)


def bar_segs(value, vmax, width, color_bg="42"):
    """Bar drawn with coloured spaces (works in any console font)."""
    if width <= 0:
        return []
    value = 0 if value is None else max(0.0, min(float(value), float(vmax)))
    n = int(round(width * value / vmax)) if vmax else 0
    out = []
    if n:
        out.append((" " * n, color_bg))
    if width - n:
        out.append((" " * (width - n), "40;37"))
    return out


def header_line(st, cols):
    t = time.strftime("%H:%M:%S", time.localtime(st["now"]))
    host = st["host"][: max(1, cols - len(t) - 1)]
    return [seg(host, CYAN), seg(" " * max(1, cols - len(host) - len(t)), DIM), seg(t, CYAN)]


def lines_for(st, cols):
    """Return lines in priority order."""
    L = [header_line(st, cols)]

    if st["decoder_ok"]:
        L.append([seg("NOW  ", DIM), seg(st["planes"], BOLD_WHITE), seg(" planes  ", DIM),
                  seg(st["with_pos"], BOLD_WHITE), seg(" w/pos", DIM)])
    else:
        L.append([seg("NOW  decoder not running", RED)])

    def num(v):
        return "--" if v is None else str(v)

    L.append([seg("TODAY    ", DIM), seg(num(st["today"]), BOLD_WHITE), seg(" unique", DIM)])
    L.append([seg("ALL-TIME ", DIM), seg(num(st["alltime"]), BOLD_WHITE), seg(" unique", DIM)])

    L.append([ok_seg("DONGLE", st["dongle"]), seg(" "), ok_seg("DEC", st["decoder_ok"]),
              seg(" "), ok_seg("LOG", st["logger_ok"])])

    if st["last_icao"]:
        age = ago(st["now"] - st["last_ts"]) if st["last_ts"] else "?"
        L.append([seg("NEW  ", DIM), seg(st["last_icao"], BOLD_WHITE), seg(f"  {age} ago", DIM)])
    else:
        L.append([seg("NEW  none yet", DIM)])

    rate = st["rate"]
    label = "MSG/s " + ("--" if rate is None else f"{rate:.0f}")
    w = max(0, cols - len(label) - 1)
    L.append([seg(label + " ", DIM)] + bar_segs(rate, 150, min(w, 24)))

    if st["best_rssi"] is not None:
        L.append([seg("SIG  best ", DIM), seg(f"{st['best_rssi']:.1f}", BOLD_WHITE), seg(" dBFS", DIM)])

    clk = st["clock"]
    if clk is None:
        clk_seg = seg("CLOCK ?", DIM)
    elif clk == "ok":
        clk_seg = seg("CLOCK ok", GREEN)
    else:
        clk_seg = seg("CLOCK UNSYNCED", RED)
    thr = st["throttled"]
    pwr = seg("  PWR ?", DIM) if thr is None else (seg("  PWR LOW", RED) if thr else seg("  PWR ok", GREEN))
    L.append([clk_seg, pwr])

    L.append([seg("IP " + (st["ip"] or "no network"), GREEN if st["ip"] else RED)])

    parts = []
    if st["free"] is not None:
        parts.append(f"{st['free']:.0f}G free")
    if st["temp"] is not None:
        parts.append(f"{st['temp']:.0f}C")
    if parts:
        L.append([seg("SD " + "  ".join(parts), DIM)])
    return L


def fit(line, cols):
    """Truncate a line to cols visible characters; return ANSI string."""
    out, used = [], 0
    for text, sgr in line:
        if used >= cols:
            break
        text = text[: cols - used]
        used += len(text)
        out.append(f"\033[{sgr}m{text}\033[0m")
    return "".join(out)


def strip_ansi(s):
    return re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", s)


def load_ui(path, now):
    """Page/message/hold state written by the button daemon. Anything missing or stale is ignored."""
    ui = {"page": 0, "msg": "", "hold": None, "wifi": None, "bias": None}
    d = read_json(path)
    if not isinstance(d, dict):
        return ui
    try:
        ui["page"] = int(d.get("page", 0)) % NPAGES
    except (TypeError, ValueError):
        pass
    if (isinstance(d.get("msg"), str) and isinstance(d.get("msg_until"), (int, float))
            and now < d["msg_until"]):
        ui["msg"] = d["msg"]
    h = d.get("hold")
    if (isinstance(h, dict) and isinstance(h.get("label"), str)
            and isinstance(h.get("progress"), (int, float))
            and isinstance(d.get("ts"), (int, float)) and abs(now - d["ts"]) < 5):
        ui["hold"] = {"label": h["label"], "progress": max(0.0, min(1.0, float(h["progress"])))}
    for k in ("wifi", "bias"):
        if isinstance(d.get(k), str) and d[k]:
            ui[k] = d[k]
    return ui


def net_lines(st, ui, cols):
    """Page 2: network and system."""
    L = [header_line(st, cols), [seg("PAGE 2/%d  NETWORK" % NPAGES, CYAN)]]
    wifi = ui.get("wifi")
    L.append([seg("WIFI ", DIM), seg(wifi or "unknown", GREEN if wifi and "not connected" not in wifi else RED)])
    L.append([seg("IP " + (st["ip"] or "no network"), GREEN if st["ip"] else RED)])
    clk = st["clock"]
    L.append([seg("CLOCK ", DIM),
              seg("?", DIM) if clk is None else (seg("ok", GREEN) if clk == "ok" else seg("UNSYNCED", RED))])
    bias = ui.get("bias")
    L.append([seg("BIAS TEE ", DIM),
              seg(bias or "unknown", YELLOW if bias == "ON" else (GREEN if bias == "OFF" else DIM))])
    L.append([ok_seg("DONGLE", st["dongle"]), seg(" "), ok_seg("DEC", st["decoder_ok"]),
              seg(" "), ok_seg("LOG", st["logger_ok"])])
    thr = st["throttled"]
    L.append([seg("PWR ", DIM), seg("?", DIM) if thr is None else (seg("LOW", RED) if thr else seg("ok", GREEN))])
    parts = []
    if st["free"] is not None:
        parts.append(f"{st['free']:.0f}G free")
    if st["temp"] is not None:
        parts.append(f"{st['temp']:.0f}C")
    if parts:
        L.append([seg("SD " + "  ".join(parts), DIM)])
    return L


LEGEND = "1:page 2:bias* 3:off* 4:sync  *=hold"


def guide_lines(ui, cols, rows):
    """Four labelled cells, one per button (1..4, left to right). Two rows when there is room:
    what the button does, and whether it is a tap or a 3 s hold. Cell 2 shows what holding it will do."""
    bias = ui.get("bias")
    # the bias label names what the button will DO (shows ON while it is off); the cell is yellow while it is on
    bias_label = {"ON": "BIAS OFF", "OFF": "BIAS ON"}.get(bias, "BIAS ?")
    names = ["PAGE %d/%d" % (ui["page"] + 1, NPAGES), bias_label, "POWER OFF", "SD COPY"]
    how = ["tap", "hold 3s", "hold 3s", "tap"]
    cell = max(1, cols // 4)
    widths = [cell, cell, cell, max(1, cols - 3 * cell)]
    sgr_a = ["30;47", "30;43" if bias == "ON" else "30;46", "30;41", "30;46"]
    sgr_b = ["37;100", "37;100", "37;100", "37;100"]
    line_a = [seg(names[i][:widths[i]].center(widths[i]), sgr_a[i]) for i in range(4)]
    if rows < 12:
        return [line_a]
    line_b = [seg(how[i][:widths[i]].center(widths[i]), sgr_b[i]) for i in range(4)]
    return [line_a, line_b]


def compose(st, ui, cols, rows, guide="top"):
    """Body (page 1 or 2), the button guide (top, bottom or off), and a hold bar or message."""
    body = lines_for(st, cols) if ui["page"] == 0 else net_lines(st, ui, cols)
    gl = guide_lines(ui, cols, rows) if guide in ("top", "bottom") else []
    if ui["hold"]:
        prefix = ui["hold"]["label"] + " "
        w = max(4, cols - len(prefix) - 2)
        mid = [[seg(prefix, YELLOW), seg("[", DIM)] + bar_segs(ui["hold"]["progress"], 1.0, w, "43") + [seg("]", DIM)]]
    elif ui["msg"]:
        import textwrap
        wrapped = textwrap.wrap(ui["msg"], max(1, cols)) or [""]
        mid = [[seg(w_.ljust(cols), "1;37;44")] for w_ in wrapped[:3]]
    else:
        mid = [] if gl else [[seg(LEGEND, DIM)]]
    head, tail = (gl, mid) if guide == "top" else ([], mid + gl)
    room = max(0, rows - len(head) - len(tail))
    body = body[:room]
    body = body + [[] for _ in range(room - len(body))]
    return [fit(l, cols) for l in (head + body + tail)][-rows:]


def render_lines(st, cols, rows, ui=None, guide="top"):
    if ui is None:
        return [fit(l, cols) for l in lines_for(st, cols)[:rows]]
    return compose(st, ui, cols, rows, guide)


def color_bars(cols, rows):
    names = [("BLACK", "40", "37"), ("RED", "41", "37"), ("GREEN", "42", "30"),
             ("YELLOW", "43", "30"), ("BLUE", "44", "37"), ("MAGENTA", "45", "37"),
             ("CYAN", "46", "30"), ("WHITE", "47", "30")]
    cell = max(3, cols // len(names))
    out = []
    head = "COLOUR TEST: each bar's label must match its colour"
    out.append(f"\033[1;37m{head[:cols]}\033[0m")
    bar_rows = max(1, min(rows - 3, 8))
    for r in range(bar_rows):
        row = ""
        for name, bg, fg in names:
            label = name[: cell - 1].center(cell) if r == bar_rows // 2 else " " * cell
            row += f"\033[{bg};{fg}m{label}\033[0m"
        out.append(row)
    out.append("\033[1;37mgrey ramp:\033[0m")
    ramp = ""
    for bg in ("40", "100", "47"):
        ramp += f"\033[{bg}m{' ' * max(1, cols // 3)}\033[0m"
    out.append(ramp)
    return out[:rows]


def frame(lines, rows):
    """Cursor-addressed frame (no scrolling, no full clear = no flicker)."""
    buf = ["\033[?25l"]
    for i, ln in enumerate(lines):
        buf.append(f"\033[{i + 1};1H{ln}\033[K")
    buf.append(f"\033[{len(lines) + 1};1H\033[J")
    return "".join(buf)


def console_size(default=(40, 15)):
    try:
        sz = os.get_terminal_size(sys.stdout.fileno())
        if sz.columns > 0 and sz.lines > 0:
            return sz.columns, sz.lines
    except OSError:
        pass
    return default


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plain", action="store_true")
    ap.add_argument("--colors", action="store_true")
    ap.add_argument("--hold", type=float, default=0.0)
    ap.add_argument("--cols", type=int)
    ap.add_argument("--rows", type=int)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--aircraft-json", default=AIRCRAFT_JSON)
    ap.add_argument("--status-json", default=STATUS_JSON)
    ap.add_argument("--ui-json", default=UI_JSON)
    ap.add_argument("--guide", choices=["top", "bottom", "off"], default="top",
                    help="where to draw the button guide (match it to where the buttons are)")
    ap.add_argument("--data-dir", default=DATA_DIR)
    a = ap.parse_args(argv)

    dcols, drows = console_size()
    cols, rows = a.cols or dcols, a.rows or drows

    if a.colors:
        lines = color_bars(cols, rows)
        if a.plain:
            print("\n".join(strip_ansi(l) for l in lines))
            return 0
        sys.stdout.write("\033[2J" + frame(lines, rows))
        sys.stdout.flush()
        time.sleep(a.hold)
        return 0

    col = Collector(a.aircraft_json, a.status_json, a.data_dir)
    if a.plain:
        col.collect()          # prime the message-rate baseline
        time.sleep(0.01)
        lines = render_lines(col.collect(), cols, rows)
        print("\n".join(strip_ansi(l) for l in lines))
        return 0

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    # clear, never blank the screen, unblank now
    sys.stdout.write("\033[2J\033[9;0]\033[13]")
    st, next_full, last_sig = None, 0.0, None
    while not stop["v"]:
        try:
            ui = load_ui(a.ui_json, time.time())
            sig = json.dumps(ui, sort_keys=True)
            full = st is None or time.monotonic() >= next_full
            if full or sig != last_sig:
                if full:
                    st = col.collect()
                    next_full = time.monotonic() + a.interval
                dcols, drows = console_size()
                c, r = a.cols or dcols, a.rows or drows
                sys.stdout.write(frame(render_lines(st, c, r, ui, a.guide), r))
                sys.stdout.flush()
                last_sig = sig
        except Exception as e:  # keep running, journal shows the reason
            print(f"display error: {e!r}", file=sys.stderr, flush=True)
        time.sleep(0.2)
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
