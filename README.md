# ads-b

ADS-B tools by FusedStamen.

| Folder | What it is |
|---|---|
| [`ads-b_logger`](ads-b_logger) | Archive logger: polls a dump1090-fa / readsb / tar1090 `aircraft.json` feed and builds a daily JSON archive of every unique aircraft seen. Windows or Linux, Python standard library only |
| [`pi-zero-logger`](pi-zero-logger) | A headless Raspberry Pi Zero 2 W logger for WatchDogs Go (WDGoWars): unique aircraft logged to the SD card in the game's CSV format, optional signed upload, saved WiFi networks, RTL-SDR Blog V4 bias tee switch, and optional Argon POD screen and buttons |

Released under the MIT license (see `LICENSE`).
