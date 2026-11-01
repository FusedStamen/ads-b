# Pi Zero 2 W ADS-B logger for WatchDogs Go

A small, headless ADS-B logger built for the WatchDogs Go (WDGoWars) game. It runs on a Raspberry Pi
Zero 2 W with an RTL-SDR, keeps **one row per aircraft (ICAO) with a position** in the game's own CSV
format, and has no feeders (no FlightAware or ADSBexchange). It was built to replace carrying a bigger
device when travelling.

**Status:** built up step by step on a fresh Raspberry Pi OS Trixie Lite (64-bit) install on a Pi Zero 2 W
with an RTL-SDR Blog V4, and working there. The logic has unit tests (synthetic data, run on a normal PC).
It has not been tested on other hardware or OS versions. The setup scripts change system files; each has an
`undo` mode, but use this at your own risk.

## What you need

- Raspberry Pi Zero 2 W (2.4 GHz WiFi only) and a good endurance SD card
- An RTL-SDR dongle and a 1090 MHz antenna. The bias tee switch needs an **RTL-SDR Blog V4**.
- Optional: an [Argon POD](https://argon40.com) case with its 2.8" screen module for the status screen and the four buttons

## What is in the folder

Core, works on any Pi with an RTL-SDR:

| File | Purpose |
|---|---|
| `adsb_unique_logger.py`, `adsb-unique-logger.service` | Polls dump1090-fa, writes `adsb_aircraft.csv` (game format, one row per ICAO) and daily summaries to `/var/lib/adsb-zero` |
| `adsb_sdexport_setup.sh` | Copies the logs to the SD card's FAT boot partition every 5 minutes, so a Windows PC can read them from the card (drive `bootfs`, folder `adsb-export`) |
| `adsb_wifi_setup.sh` | Several saved WiFi networks with priorities (home, phone hotspot...). Add them over SSH or from a text file on the SD card |
| `adsb_upload.py`, `adsb_upload_setup.sh` | Optional upload to WDGoWars using the same signed JSON the game's plugin uses. Sends only ICAOs not sent before |
| `validate_for_game.py` | Checks the CSV against the game's row rules and prints what would be uploadable |
| `adsb_biast_setup.sh` | Switch the V4's bias tee on or off from the Pi (off by default) |
| `merge_adsb.py`, `pull_zero.sh` | Optional: pull the logs over WiFi to another machine and merge with another receiver's CSVs |
| `test_*.py` | Unit tests |

Argon POD add-on:

| File | Purpose |
|---|---|
| `pod_display_setup.sh` | Gets the POD's 2.8" screen working as a text console on Trixie, without Argon's own installer |
| `adsb_display.py`, `adsb_display_setup.sh` | Two-page live status screen (aircraft stats, then network, clock and bias tee state) with a button guide |
| `adsb_buttons.py`, `adsb_buttons_setup.sh` | The four buttons: page, bias tee (hold 3 s), safe shutdown (hold 3 s, copies logs first), copy logs to the SD card |

## Install

Copy this folder to the Pi, for example `scp -r pi-zero-logger YOU@HOSTNAME.local:~/`, and run the scripts from inside it.

**1. Flash and first boot.** Raspberry Pi Imager, Raspberry Pi OS Lite 64-bit. In the settings set a hostname,
username, SSH and your 2.4 GHz WiFi. Then:

```
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y rtl-sdr python3 rsync
printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo reboot
```

**2. Decoder only (no feeder).** Install dump1090-fa from FlightAware's apt repository, but not `piaware`:

```
wget https://www.flightaware.com/adsb/piaware/files/packages/pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.2_all.deb
sudo dpkg -i flightaware-apt-repository_1.2_all.deb
sudo apt update && sudo apt install -y dump1090-fa
lsusb | grep -i rtl
python3 -c "import json;d=json.load(open('/run/dump1090-fa/aircraft.json'));a=d['aircraft'];print(len(a),'aircraft,',sum('lat' in x for x in a),'with position')"
```

The defaults work. Gain and adaptive settings are in `/etc/default/dump1090-fa`.

**3. Optional, gentler on the SD card.**

```
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' | sudo tee /etc/systemd/journald.conf.d/volatile.conf
sudo systemctl restart systemd-journald
sudo systemctl disable --now dphys-swapfile 2>/dev/null
```

**4. Install the logger.**

```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin adsblog
sudo install -d /opt/adsb-zero
sudo install -m 755 adsb_unique_logger.py /opt/adsb-zero/
sudo install -m 644 adsb-unique-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adsb-unique-logger
sudo journalctl -u adsb-unique-logger -n 20 --no-pager
```

Optional: to record each aircraft's farthest position and range from a fixed antenna, put your real position in
`/etc/default/adsb-zero` as `LOGGER_ARGS="--lat 12.345678 --lon -98.765432"`. Use real numbers or leave the file out.
Without it the logger still works. A mobile receiver should leave it out.

**5. Optional parts.** Each script prints its usage when run with no arguments:

```
sudo bash adsb_sdexport_setup.sh install     # logs on the SD card, readable on Windows
sudo bash adsb_wifi_setup.sh add             # saved WiFi networks (asks for name, password, priority)
sudo bash adsb_upload_setup.sh setkey        # your WDGoWars API key, then: test, dry, upload
sudo bash adsb_biast_setup.sh install        # V4 bias tee switch (builds a small tool, needs internet)
python3 validate_for_game.py                 # check the CSV against the game's rules
```

**6. Argon POD (optional).**

```
sudo bash pod_display_setup.sh install       # then reboot; downloads Argon's screen overlay and checks its hash
sudo bash adsb_display_setup.sh install      # status screen
sudo bash adsb_buttons_setup.sh install      # buttons (needs the two above)
```

Buttons, left to right on the case (GPIO 16, 20, 21, 26): 1 changes the page, 2 held for 3 seconds toggles the
bias tee, 3 held for 3 seconds exports the logs and powers off safely, 4 copies the logs to the SD card now.
Run `sudo bash adsb_display_setup.sh guide top|bottom|off` to put the on-screen button guide where your buttons are.

## Output

In `/var/lib/adsb-zero`:

- `adsb_aircraft.csv` - the game's format, one row per ICAO ever seen, with the first valid position
- `summary/summary_YYYY-MM-DD.csv` - per day: first and last seen, first and best (farthest) position, max altitude and signal

Only aircraft with a position are logged, since the game ignores rows without one.

## Uploading

`adsb_upload_setup.sh` uses the game's signed-JSON upload. Your API key is typed on the device and stored only in
`/etc/adsb-zero/wdgwars.key` (root-only); it is never printed or logged. Only ICAOs not sent before are uploaded,
and the server de-duplicates per aircraft. The Pi needs internet for this; a phone hotspot works. You can also
just pull the SD card (or copy `adsb-export/adsb_aircraft.csv` from `bootfs`) and upload from a PC.

## Clock

The Pi Zero has no battery-backed clock. Offline it restores roughly the last known time, and syncs as soon as it
joins WiFi. Rows logged before a sync are tagged in the daily summaries, and the logger corrects their timestamps
if the clock jumps while it keeps running. The uploader skips rows dated before 2024 or in the future, so an
upload from a Pi with a badly wrong clock can miss aircraft. Join a phone hotspot once so it syncs.

## Bias tee safety

The V4's bias tee puts about 4.5 V on the antenna connector. Turn it on only with a bias-tee powered preamp between
the antenna and the dongle. It is off by default, and the decoder switches it off every time it stops.

## Tests

```
python3 test_adsb_logger.py
python3 test_adsb_upload.py
python3 test_validate_for_game.py
python3 test_merge_adsb.py
python3 test_adsb_display.py
python3 test_adsb_buttons.py
```

## License

MIT, see the `LICENSE` file at the top of the repository.
