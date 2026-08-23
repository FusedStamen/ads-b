#!/usr/bin/env python3
"""
adsb_archive_logger.py — Continuously poll a dump1090 (or dump1090-fa /
readsb / tar1090) aircraft.json feed and accumulate unique aircraft into a
daily JSON archive.

Platform-agnostic: works on Windows, macOS, and Linux. Uses only the Python
standard library (no pip installs required).

Runs as a long-lived process, polling on an interval. Each unique ICAO hex
is recorded once per calendar day, with a first-seen timestamp and the best
available position/telemetry data merged in as it becomes available.

Usage:
    python3 adsb_archive_logger.py
    python3 adsb_archive_logger.py --interval 15 --output-dir ./my-archive
    python3 adsb_archive_logger.py --url http://192.168.1.50:8080/data/aircraft.json

By default this polls http://localhost:8080/data/aircraft.json, which is
the standard URL for dump1090-fa, readsb, and tar1090 web UIs. If your
receiver is on another machine, point --url at it (see the setup guide).
"""

import json
import os
import time
import argparse
import signal
import sys
import platform
from datetime import datetime, date
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_URL = "http://localhost:8080/data/aircraft.json"

# Default archive location lives under the user's home directory so this
# works out of the box on Windows, macOS, and Linux without editing paths.
DEFAULT_OUTPUT_DIR = str(Path.home() / "adsb-archive")

DEFAULT_INTERVAL = 30  # seconds between polls

# Common on-disk locations for the aircraft.json file, used as a fallback
# if the HTTP URL can't be reached (e.g. running this script directly on
# the Pi/SDR host). Windows installs of dump1090 typically don't expose a
# local file path like this, so this list is Linux/macOS-oriented; Windows
# users should rely on --url instead.
LOCAL_FALLBACK_PATHS = [
    "/run/dump1090-fa/aircraft.json",
    "/run/readsb/aircraft.json",
    "/var/run/dump1090-fa/aircraft.json",
]

# ── Globals ───────────────────────────────────────────────────────────────────
running = True


def signal_handler(sig, frame):
    global running
    print("\nShutting down gracefully...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
# SIGTERM isn't available on Windows in all contexts, so guard it.
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal_handler)


def fetch_aircraft(url: str) -> list:
    """Fetch aircraft list from an HTTP endpoint, falling back to known
    local file paths (Linux/macOS only) if the request fails."""
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            return data.get("aircraft", [])
    except Exception:
        pass

    if platform.system() != "Windows":
        for path in LOCAL_FALLBACK_PATHS:
            try:
                if os.path.exists(path):
                    with open(path) as f:
                        data = json.load(f)
                        return data.get("aircraft", [])
            except Exception:
                continue

    print("  ERROR: could not reach aircraft feed via URL or local fallback paths")
    return []


def get_output_path(output_dir: str) -> Path:
    """Get today's output file path."""
    today = date.today().strftime("%Y-%m-%d")
    return Path(output_dir) / f"aircraft_{today}.json"


def load_existing(path: Path) -> dict:
    """Load existing daily archive or return empty dict."""
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            return {a["hex"]: a for a in data.get("aircraft", []) if a.get("hex")}
        except Exception as e:
            print(f"  WARNING: Could not load existing archive: {e}")
    return {}


def save_archive(path: Path, hexes: dict):
    """Write accumulated aircraft to daily JSON (atomic replace)."""
    today = date.today().strftime("%Y-%m-%d")
    out = {
        "now": time.time(),
        "date": today,
        "unique_aircraft_count": len(hexes),
        "aircraft": sorted(hexes.values(), key=lambda x: x.get("first_seen", 0)),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, path)


def process_aircraft(aircraft_list: list, hexes: dict, now: float) -> int:
    """Merge new aircraft into accumulated dict. Returns count of new additions."""
    new_count = 0
    for a in aircraft_list:
        hex_id = a.get("hex", "").lower().strip()
        if not hex_id:
            continue

        if hex_id not in hexes:
            entry = {
                "hex": hex_id,
                "flight": a.get("flight", "").strip() if a.get("flight") else None,
                "lat": a.get("lat"),
                "lon": a.get("lon"),
                "alt_baro": a.get("alt_baro"),
                "alt_geom": a.get("alt_geom"),
                "gs": a.get("gs"),
                "track": a.get("track"),
                "baro_rate": a.get("baro_rate"),
                "squawk": a.get("squawk"),
                "category": a.get("category"),
                "rssi": a.get("rssi"),
                "messages": a.get("messages", 0),
                "first_seen": now,
                "last_seen": now,
            }
            hexes[hex_id] = entry
            new_count += 1
        else:
            existing = hexes[hex_id]
            existing["last_seen"] = now
            existing["messages"] = max(existing.get("messages", 0), a.get("messages", 0))

            if not existing.get("flight") and a.get("flight"):
                existing["flight"] = a["flight"].strip()
            if existing.get("lat") is None and a.get("lat") is not None:
                existing["lat"] = a["lat"]
                existing["lon"] = a["lon"]
            if existing.get("alt_baro") is None and a.get("alt_baro") is not None:
                existing["alt_baro"] = a["alt_baro"]
            if existing.get("squawk") is None and a.get("squawk") is not None:
                existing["squawk"] = a["squawk"]

    return new_count


def main():
    parser = argparse.ArgumentParser(description="Continuous ADS-B aircraft.json archive logger")
    parser.add_argument("--url", default=DEFAULT_URL, help="aircraft.json URL (dump1090-fa/readsb/tar1090)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Output directory for daily JSON files")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL, type=int, help="Poll interval in seconds")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("ADS-B Archive Logger")
    print(f"  Platform: {platform.system()}")
    print(f"  URL:      {args.url}")
    print(f"  Output:   {output_dir}")
    print(f"  Interval: {args.interval}s")
    print("  Press Ctrl+C to stop\n")

    current_day = date.today()
    output_path = get_output_path(str(output_dir))
    hexes = load_existing(output_path)
    print(f"Loaded {len(hexes)} existing aircraft from {output_path}")

    poll_count = 0

    while running:
        now = time.time()

        # Handle day rollover
        if date.today() != current_day:
            print(f"\nDay rollover — saving {output_path}")
            save_archive(output_path, hexes)
            current_day = date.today()
            output_path = get_output_path(str(output_dir))
            hexes = {}
            print(f"New day: {output_path}\n")

        aircraft_list = fetch_aircraft(args.url)
        new = process_aircraft(aircraft_list, hexes, now)
        poll_count += 1

        if new > 0 or poll_count % 10 == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] Poll #{poll_count}: {len(aircraft_list)} visible, "
                  f"+{new} new, {len(hexes)} total today")

        if poll_count % 5 == 0:
            save_archive(output_path, hexes)

        time.sleep(args.interval)

    print(f"Saving final archive: {output_path}")
    save_archive(output_path, hexes)
    print(f"Done. {len(hexes)} unique aircraft logged.")


if __name__ == "__main__":
    main()
