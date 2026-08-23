# ADS-B Archive Logger — Setup Guide (Windows & Linux)

This script polls a dump1090-fa / readsb / tar1090 `aircraft.json` feed on
an interval and builds a daily JSON archive of every unique aircraft seen,
with first-seen timestamp, position, and telemetry. It uses only Python's
standard library - no `pip install` required.

## 1. Prerequisites

- **Python 3.7+** installed and on your `PATH`.
  - Windows: install from [python.org](https://www.python.org/downloads/) and check "Add python.exe to PATH" during install.
  - Linux: `python3 --version` - almost always already present; if not, `sudo apt install python3`.
- A running ADS-B receiver exposing an `aircraft.json` HTTP feed. This is
  the default output of **dump1090-fa**, **readsb**, and **tar1090** - the
  feed is normally reachable at:

  ```
  http://<receiver-ip>:8080/data/aircraft.json
  ```

  If the script runs on the *same machine* as the receiver, `<receiver-ip>`
  is `localhost`. If it runs on a *different machine* on your network
  (e.g. logging from a laptop while a Raspberry Pi runs the receiver),
  use that machine's IP address instead — see step 4.

## 2. Get the script

Save `adsb_archive_logger.py` anywhere convenient:

- Windows: e.g. `C:\Users\<you>\adsb-archive-logger\adsb_archive_logger.py`
- Linux: e.g. `~/adsb-archive-logger/adsb_archive_logger.py`

No other files are needed - it's a single script.

## 3. Run it

Open a terminal (Command Prompt / PowerShell on Windows, or any shell on
Linux) in the folder where you saved the script:

```bash
python3 adsb_archive_logger.py
```

On Windows, use `python` instead of `python3` if that's how your install
registered it:

```powershell
python adsb_archive_logger.py
```

By default it will:
- Poll `http://localhost:8080/data/aircraft.json` every 30 seconds
- Write daily archive files to a folder named `adsb-archive` in your home
  directory (`~/adsb-archive` on Linux, `C:\Users\<you>\adsb-archive` on
  Windows) - this is created automatically
- Print a running log of new aircraft seen, and save progress every 5 polls

Press `Ctrl+C` to stop; it will do a final save before exiting.

## 4. Common configuration

All options are optional flags:

| Flag | Default | Purpose |
|---|---|---|
| `--url` | `http://localhost:8080/data/aircraft.json` | Where to fetch aircraft data from |
| `--output-dir` | `~/adsb-archive` | Where daily JSON files are written |
| `--interval` | `30` | Seconds between polls |

**Logging from a receiver on another machine** (e.g. a Pi on your LAN):

```bash
python3 adsb_archive_logger.py --url http://192.168.1.50:8080/data/aircraft.json
```

Find the receiver's IP from its own network settings, or by checking
whatever device is running dump1090-fa/readsb/tar1090.

**Custom output location:**

```bash
python3 adsb_archive_logger.py --output-dir "D:\ADSB Logs"
```

```bash
python3 adsb_archive_logger.py --output-dir /home/pi/adsb-logs
```

**Faster/slower polling:**

```bash
python3 adsb_archive_logger.py --interval 15
```

## 5. Verifying it's working

Within a few seconds you should see console output like:

```
ADS-B Archive Logger
  Platform: Linux
  URL:      http://localhost:8080/data/aircraft.json
  Output:   /home/you/adsb-archive
  Interval: 30s
  Press Ctrl+C to stop

Loaded 0 existing aircraft from /home/you/adsb-archive/aircraft_2026-08-23.json
[14:02:10] Poll #1: 6 visible, +6 new, 6 total today
```

If instead you see `ERROR: could not reach aircraft feed via URL or local
fallback paths`, double check:
- The receiver software (dump1090-fa/readsb/tar1090) is actually running
- The URL/port match what your receiver's web UI uses (open `--url` in a
  browser — you should see raw JSON)
- No firewall is blocking the connection if the receiver is on another
  machine

## 6. Running it continuously (optional)

### Windows — Task Scheduler
1. Open **Task Scheduler** → *Create Task*
2. **General**: name it, check "Run whether user is logged on or not"
3. **Triggers**: New → "At startup" (or "At log on")
4. **Actions**: New → Program/script: `python`, arguments:
   `C:\path\to\adsb_archive_logger.py --output-dir "C:\ADSB Logs"`
5. Save. It will now start automatically and keep running in the background.

### Linux — systemd service
Create `/etc/systemd/system/adsb-archive.service`:

```ini
[Unit]
Description=ADS-B Archive Logger
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/you/adsb-archive-logger/adsb_archive_logger.py
Restart=on-failure
User=you

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now adsb-archive.service
sudo journalctl -u adsb-archive.service -f   # to watch logs
```

## 7. Output format

Each day gets one file, `aircraft_YYYY-MM-DD.json`, structured as:

```json
{
  "now": 1755936000.0,
  "date": "2026-08-23",
  "unique_aircraft_count": 42,
  "aircraft": [
    {
      "hex": "a1b2c3",
      "flight": "UAL123",
      "lat": 42.37,
      "lon": -71.05,
      "alt_baro": 35000,
      "alt_geom": 35200,
      "gs": 480,
      "track": 270,
      "baro_rate": 0,
      "squawk": "1200",
      "category": "A3",
      "rssi": -12.3,
      "messages": 154,
      "first_seen": 1755930000.0,
      "last_seen": 1755935990.0
    }
  ]
}
```

Fields are merged in as they become available across polls (e.g. an
aircraft's position may be `null` until it reports a valid lat/lon), and
each hex only appears once per day regardless of how many times it's seen.
