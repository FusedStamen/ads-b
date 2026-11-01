#!/usr/bin/env python3
"""
adsb_upload.py - upload the Pi's ADS-B aircraft to WDGoWars using the same signed-JSON protocol
the WatchDogsGo plugin (wardrive_upload.py v2.1) uses.

  POST https://wdgwars.pl/api/upload/
  headers: Content-Type: application/json, X-API-Key: <key>, Accept: application/json,
           User-Agent: WatchDogsGo/<version> (<platform>; Python/x.y)   (Cloudflare blocks default agents)
  body:    {"data": base64(json({"networks":[],"aircraft":[...],"meshcore_nodes":[]})),
            "nonce": <16 hex>, "sig": HMAC-SHA256(key, nonce + data_b64) hex}
  reply:   {"aircraft_imported": N, "aircraft_already_seen": M, ...}

Reads the CSV the logger writes, applies the game's row rules, sends only ICAOs not sent before
(tracked in a state file), in chunks. The key is read from a private file and never printed.

  python3 adsb_upload.py --test        check the key and connection with an empty signed upload
  python3 adsb_upload.py --dry-run     show what would be sent, send nothing
  python3 adsb_upload.py               upload new aircraft
"""
import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = "1.0"
DEFAULT_URL = "https://wdgwars.pl/api/upload/"
DEFAULT_CSV = "/var/lib/adsb-zero/adsb_aircraft.csv"
DEFAULT_STATE = "/var/lib/adsb-zero/uploaded_icaos.txt"
DEFAULT_KEY = "/etc/adsb-zero/wdgwars.key"
MIN_TS = 1704067200      # 2024-01-01: older means the Pi clock was wrong when logged
CHUNK = 500


def user_agent():
    return "WatchDogsGo/adsb-zero-%s (PiZero2W; Python/%d.%d)" % (
        VERSION, sys.version_info.major, sys.version_info.minor)


def sign_payload(data, key, nonce=None):
    """Exactly the game's _sign_payload."""
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    nonce = nonce or secrets.token_hex(8)
    data_b64 = base64.b64encode(raw).decode("ascii")
    sig = hmac.new(key.encode("utf-8"), (nonce + data_b64).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return json.dumps({"data": data_b64, "nonce": nonce, "sig": sig}).encode("utf-8")


def parse_csv(path, now=None):
    """The game's _parse_adsb row rules, plus a clock sanity check. Returns (aircraft, skipped)."""
    now = now or time.time()
    aircraft, skipped = {}, {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    with open(path, "r", encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        if not next(rd, None):
            return [], skipped
        for parts in rd:
            if len(parts) < 8:
                skip("short_row")
                continue
            try:
                lat = float(parts[3]) if parts[3] else 0.0
                lon = float(parts[4]) if parts[4] else 0.0
            except ValueError:
                skip("bad_position")
                continue
            if (lat == 0.0 and lon == 0.0) or lat < -90 or lat > 90 or lon < -180 or lon > 180:
                skip("no_valid_position")
                continue
            icao = parts[1]
            if not icao or icao in aircraft:
                continue
            try:
                ts = float(parts[0])
            except ValueError:
                skip("bad_timestamp")
                continue
            if ts < MIN_TS or ts > now + 86400:
                skip("bad_clock")
                continue
            try:
                alt = int(float(parts[5])) if parts[5] else 0
                spd = int(float(parts[6])) if parts[6] else 0
                hdg = int(float(parts[7])) if parts[7] else 0
            except ValueError:
                alt = spd = hdg = 0
            aircraft[icao] = {"icao": icao, "callsign": parts[2], "lat": lat, "lon": lon,
                              "alt_ft": alt, "speed_kt": spd, "heading": hdg,
                              "first_seen": parts[0], "type": "ADSB"}
    return list(aircraft.values()), skipped


def load_sent(state):
    try:
        with open(state) as f:
            return {l.strip() for l in f if l.strip()}
    except FileNotFoundError:
        return set()


def save_sent(state, icaos):
    os.makedirs(os.path.dirname(state) or ".", exist_ok=True)
    with open(state, "a") as f:
        f.write("".join(i + "\n" for i in icaos))
        f.flush()
        os.fsync(f.fileno())


def read_key(path):
    try:
        with open(path) as f:
            key = f.read().strip()
    except FileNotFoundError:
        raise SystemExit("No key file at %s. Run:  sudo bash adsb_upload_setup.sh setkey" % path)
    except PermissionError:
        raise SystemExit("Cannot read %s - run with sudo." % path)
    if len(key) < 8 or any(c.isspace() for c in key):
        raise SystemExit("The key file looks wrong (empty, too short, or contains spaces).")
    return key


def post(url, key, data, timeout=90):
    req = Request(url, data=sign_payload(data, key), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", key)
    req.add_header("User-Agent", user_agent())
    req.add_header("Accept", "application/json")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def post_retry(url, key, data, sleep=time.sleep):
    """One retry on HTTP 429, like the game. Returns (result, error_text)."""
    for attempt in (1, 2):
        try:
            return post(url, key, data), None
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200].replace(key, "<KEY>")
            if e.code == 429 and attempt == 1:
                print("  rate limited, retrying in 5 s...")
                sleep(5)
                continue
            hint = ""
            if e.code == 403:
                hint = " (403 can mean Cloudflare blocked the request or the key/signature was refused)"
            if e.code in (401,):
                hint = " (the server did not accept the API key)"
            return None, "HTTP %d: %s%s" % (e.code, body, hint)
        except URLError as e:
            return None, "connection error: %s" % e.reason
        except (TimeoutError, OSError) as e:
            return None, "timeout/network error: %s (the server may or may not have received it)" % e
        except ValueError:
            return None, "the server replied with something that is not JSON"
    return None, "failed"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--key-file", default=DEFAULT_KEY)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--chunk", type=int, default=CHUNK)
    ap.add_argument("--resend-all", action="store_true",
                    help="ignore the sent list (the server dedups, so this is safe, just slower)")
    a = ap.parse_args(argv)

    key = read_key(a.key_file)
    print("Server:     %s" % a.url)
    print("User-Agent: %s" % user_agent())

    if a.test:
        res, err = post_retry(a.url, key, {"networks": []})
        if err:
            print("TEST FAILED: %s" % err)
            return 1
        total = res.get("total", res.get("total_records", "?"))
        print("TEST OK: the server accepted the key and the signature. Reply: %s" %
              json.dumps({k: v for k, v in res.items() if isinstance(v, (int, str, float))})[:300])
        return 0

    if not os.path.isfile(a.csv):
        print("No CSV at %s (is the logger running?)" % a.csv)
        return 1
    aircraft, skipped = parse_csv(a.csv)
    sent = set() if a.resend_all else load_sent(a.state)
    new = [x for x in aircraft if x["icao"] not in sent]
    print("Aircraft in the CSV the game would accept: %d" % len(aircraft))
    print("Already sent by this tool:                  %d" % (len(aircraft) - len(new)))
    print("New to send:                                %d" % len(new))
    if skipped:
        print("Rows skipped: " + ", ".join("%s=%d" % kv for kv in sorted(skipped.items())))
        if skipped.get("bad_clock"):
            print("  (bad_clock = logged while the Pi clock was not set; those rows are NOT uploaded)")
    if not new:
        print("Nothing to upload.")
        return 0
    if a.dry_run:
        print("DRY RUN - nothing sent. First record that would be sent:")
        print("  " + json.dumps(new[0]))
        return 0

    tot_imp = tot_seen = 0
    failed = False
    for i in range(0, len(new), a.chunk):
        batch = new[i:i + a.chunk]
        res, err = post_retry(a.url, key, {"networks": [], "aircraft": batch, "meshcore_nodes": []})
        if err:
            print("Chunk %d FAILED: %s" % (i // a.chunk + 1, err))
            failed = True
            break
        imp = int(res.get("aircraft_imported", 0) or 0)
        seen = int(res.get("aircraft_already_seen", 0) or 0)
        tot_imp += imp
        tot_seen += seen
        save_sent(a.state, [x["icao"] for x in batch])
        print("Chunk %d: sent %d -> imported %d, already seen %d%s" % (
            i // a.chunk + 1, len(batch), imp, seen,
            "" if imp + seen == len(batch) else "  (server counted %d of %d)" % (imp + seen, len(batch))))
        if i + a.chunk < len(new):
            time.sleep(1)
    print()
    print("RESULT: aircraft_imported=%d  aircraft_already_seen=%d" % (tot_imp, tot_seen))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
