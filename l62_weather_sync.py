#!/usr/bin/env python3
"""
L62 weather -> Nominal sync (v2: 1-minute updates).

Runs either on a PC (Windows Task Scheduler, once a minute) or in the cloud with --loop
(GitHub Actions: one job per hour that checks every minute). Each check:
  1. Reads the latest observation from the WeatherXM station "high-indigo-air" at L62
     (public WeatherXM explorer feed; the station publishes a new reading every minute).
  2. If the PC was off for a while, first fills the gap from the 15-minute history kept by
     https://cole581.github.io/ButtonwillowWeather/.
  3. Converts units and writes the new reading(s) to a Nominal dataset.

Channels written (all observed values, no forecast):
  L62_temp_F, L62_dewpoint_F, L62_humidity_pct, L62_wind_speed_kt, L62_wind_gust_kt,
  L62_wind_dir_deg (FROM, deg clockwise from TRUE north), L62_pressure_hPa and
  L62_pressure_inHg (STATION / absolute pressure), L62_altimeter_inHg (altimeter setting computed
  for L62 field elevation 326 ft), L62_precip_rate_in_per_hr, L62_precip_since_last_in,
  L62_precip_accum_in.

Token: python l62_weather_sync.py --set-token   (reads the Nominal API key from the clipboard;
stored in %APPDATA%\\L62_Nominal_sync\\nominal_token.txt). Never hard-code the token here.
Each run is logged to l62_sync.log next to this script.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

WXM_URL = ("https://api.weatherxm.com/api/v1/cells/8729ac626ffffff/devices/"
           "a1e49090-ad73-11ef-8b2f-f594437d6709")
HISTORY_URL = "https://raw.githubusercontent.com/cole581/ButtonwillowWeather/main/data/observations.csv"
# Nominal location of the target dataset. In the cloud these come from encrypted GitHub secrets.
NOMINAL_BASE_URL = os.environ.get("NOMINAL_BASE_URL", "https://api.gov.nominal.io/api")
WORKSPACE_RID = os.environ.get("NOMINAL_WORKSPACE_RID", "")
DATASET_RID = os.environ.get("NOMINAL_DATASET_RID", "")
TASK_NAME = "L62 Weather to Nominal"
TASK_INTERVAL_MIN = 1
FIELD_ELEV_M = 326 * 0.3048  # L62 field elevation

HERE = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("L62_STATE_FILE", HERE / "l62_sync_state.json"))
LOG_FILE = HERE / "l62_sync.log"
TOKEN_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "L62_Nominal_sync" / "nominal_token.txt"
DEFAULT_STATE = {"last_ts": "2026-11-20T17:15:00Z", "last_accum_mm": 0.0}

COLUMNS = [
    "timestamp", "L62_temp_F", "L62_dewpoint_F", "L62_humidity_pct", "L62_wind_speed_kt",
    "L62_wind_gust_kt", "L62_wind_dir_deg", "L62_pressure_hPa", "L62_pressure_inHg",
    "L62_altimeter_inHg", "L62_precip_rate_in_per_hr", "L62_precip_since_last_in",
    "L62_precip_accum_in",
]


# ----------------------------------------------------------------- helpers
def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rnd(v, nd):
    return None if v is None else round(v, nd)


def to_utc_z(ts):
    """Any ISO timestamp -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def altimeter_hpa(station_hpa):
    """NWS altimeter-setting formula from station pressure and field elevation."""
    if station_hpa is None:
        return None
    n = 0.190284
    p = station_hpa - 0.3
    return p * (1 + ((1013.25 ** n) * 0.0065 / 288.15) * (FIELD_ELEV_M / (p ** n))) ** (1 / n)


def make_row(ts, temp_c, dew_c, rh, wind_ms, gust_ms, wdir, p_hpa, rate_mmh, accum_mm, prev_accum):
    inc = None
    if accum_mm is not None and prev_accum is not None:
        inc = accum_mm - prev_accum if accum_mm >= prev_accum else accum_mm  # counter reset
    alt = altimeter_hpa(p_hpa)
    row = {
        "timestamp": ts,
        "L62_temp_F": rnd(None if temp_c is None else temp_c * 9 / 5 + 32, 2),
        "L62_dewpoint_F": rnd(None if dew_c is None else dew_c * 9 / 5 + 32, 2),
        "L62_humidity_pct": rnd(rh, 1),
        "L62_wind_speed_kt": rnd(None if wind_ms is None else wind_ms * 1.943844, 2),
        "L62_wind_gust_kt": rnd(None if gust_ms is None else gust_ms * 1.943844, 2),
        "L62_wind_dir_deg": rnd(wdir, 0),
        "L62_pressure_hPa": rnd(p_hpa, 1),
        "L62_pressure_inHg": rnd(None if p_hpa is None else p_hpa * 0.0295300, 2),
        "L62_altimeter_inHg": rnd(None if alt is None else alt * 0.0295300, 2),
        "L62_precip_rate_in_per_hr": rnd(None if rate_mmh is None else rate_mmh / 25.4, 3),
        "L62_precip_since_last_in": rnd(None if inc is None else inc / 25.4, 3),
        "L62_precip_accum_in": rnd(None if accum_mm is None else accum_mm / 25.4, 3),
    }
    new_prev = accum_mm if accum_mm is not None else prev_accum
    return row, new_prev


# ----------------------------------------------------------------- sources
def fetch_latest():
    r = requests.get(WXM_URL, timeout=20, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    cw = r.json()["current_weather"]
    return {
        "ts": to_utc_z(cw["timestamp"]),
        "temp_c": num(cw.get("temperature")), "dew_c": num(cw.get("dew_point")),
        "rh": num(cw.get("humidity")), "wind_ms": num(cw.get("wind_speed")),
        "gust_ms": num(cw.get("wind_gust")), "wdir": num(cw.get("wind_direction")),
        "p_hpa": num(cw.get("pressure")), "rate_mmh": num(cw.get("precipitation")),
        "accum_mm": num(cw.get("precipitation_accumulated")),
    }


def fetch_history():
    r = requests.get(HISTORY_URL, timeout=30)
    r.raise_for_status()
    out, seen = [], set()
    for x in sorted(csv.DictReader(io.StringIO(r.text)), key=lambda x: x.get("ts") or ""):
        if not x.get("ts") or x["ts"] in seen:
            continue
        seen.add(x["ts"])
        out.append({
            "ts": to_utc_z(x["ts"]), "temp_c": num(x["temp_c"]), "dew_c": num(x["dew_c"]),
            "rh": num(x["rh"]), "wind_ms": num(x["wind_ms"]), "gust_ms": num(x["gust_ms"]),
            "wdir": num(x["wind_dir"]), "p_hpa": num(x["pressure_hpa"]),
            "rate_mmh": num(x["precip_rate_mmh"]), "accum_mm": num(x["precip_accum_mm"]),
        })
    return out


# ----------------------------------------------------------------- token
def clean_token(raw):
    t = (raw or "").strip().strip('"').strip("'").strip()
    if t.lower().startswith("bearer "):
        t = t[7:]
    return "".join(t.split())


def looks_like_token(t):
    return bool(t) and len(t) >= 20 and re.fullmatch(r"[A-Za-z0-9\-\._~\+/]+=*", t) is not None


def get_token():
    if TOKEN_FILE.exists():
        t = clean_token(TOKEN_FILE.read_text(encoding="utf-8"))
        if t:
            return t
    return clean_token(os.environ.get("NOMINAL_TOKEN", ""))


def clipboard_text():
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout or ""
    except Exception:
        return ""


def set_token():
    have = TOKEN_FILE.exists() and looks_like_token(clean_token(TOKEN_FILE.read_text(encoding="utf-8")))
    print("      1) In Nominal, copy the API key (the long value).")
    print("      2) Come back here and just press Enter - it is read from your clipboard.")
    if have:
        print("      (A key is already saved; it is kept unless your clipboard holds a new one.)")
    raw = input("      Press Enter when copied: ")
    t = clean_token(raw) or clean_token(clipboard_text())
    if not looks_like_token(t):
        if have:
            print("      Keeping the saved key.")
            return 0
        print("      Couldn't find a Nominal key on the clipboard. Copy it and run setup.bat again.")
        return 1
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    print(f"      Key saved ({len(t)} characters).")
    return 0


# ----------------------------------------------------------------- Nominal
def get_dataset():
    from nominal.core import NominalClient
    token = get_token()
    if not token:
        raise RuntimeError("no Nominal API key saved - run setup.bat again")
    if not DATASET_RID:
        raise RuntimeError("NOMINAL_DATASET_RID is not set")
    client = NominalClient.create(NOMINAL_BASE_URL, token, workspace_rid=WORKSPACE_RID or None)
    return client.get_dataset(DATASET_RID)


def upload_rows_file(dataset, rows):
    """Bulk upload (used for gap fill) as one CSV file."""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in COLUMNS})
        path = f.name
    try:
        dataset.add_tabular_data(path, timestamp_column="timestamp", timestamp_type="iso_8601")
    finally:
        os.unlink(path)


def stream_rows(dataset, rows):
    """Small live writes (one reading per minute) via Nominal's streaming writer."""
    with dataset.get_write_stream(implementation="python") as stream:
        for r in rows:
            t = dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            vals = {k: float(r[k]) for k in COLUMNS[1:] if r[k] is not None}
            stream.enqueue_from_dict(t, vals)


# ----------------------------------------------------------------- task interval
def ensure_task_interval(state):
    """Switch the scheduled task to every TASK_INTERVAL_MIN minutes (once)."""
    if os.name != "nt" or state.get("task_interval_min") == TASK_INTERVAL_MIN:
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.name.lower() == "python.exe" else exe
    if not pyw.exists():
        pyw = exe
    tr = f'"{pyw}" "{Path(__file__).resolve()}"'
    res = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(TASK_INTERVAL_MIN),
                          "/TN", TASK_NAME, "/TR", tr], capture_output=True, text=True)
    if res.returncode == 0:
        state["task_interval_min"] = TASK_INTERVAL_MIN
        log(f"Scheduled task now runs every {TASK_INTERVAL_MIN} minute(s).")
    else:
        log(f"Could not change task interval: {(res.stderr or res.stdout).strip()}")


# ----------------------------------------------------------------- main
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return dict(DEFAULT_STATE)


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def run_once(state, dataset_getter, dry_run=False):
    """One cycle: fetch the station's latest reading and send it if new. Returns True if sent."""
    last_ts = state["last_ts"]
    prev_accum = state.get("last_accum_mm")
    latest = fetch_latest()
    if latest["ts"] <= last_ts:
        log(f"No new reading (station latest {latest['ts']}).")
        return False

    # Gap fill from the 15-minute history if more than ~3 minutes are missing.
    gap_rows = []
    gap_s = (dt.datetime.fromisoformat(latest["ts"].replace("Z", "+00:00"))
             - dt.datetime.fromisoformat(last_ts.replace("Z", "+00:00"))).total_seconds()
    if gap_s > 180:
        try:
            for h in fetch_history():
                if last_ts < h["ts"] < latest["ts"]:
                    row, prev_accum = make_row(h["ts"], h["temp_c"], h["dew_c"], h["rh"], h["wind_ms"],
                                               h["gust_ms"], h["wdir"], h["p_hpa"], h["rate_mmh"],
                                               h["accum_mm"], prev_accum)
                    gap_rows.append(row)
        except Exception as e:
            log(f"Gap fill skipped: {e}")

    row, prev_accum = make_row(latest["ts"], latest["temp_c"], latest["dew_c"], latest["rh"],
                               latest["wind_ms"], latest["gust_ms"], latest["wdir"], latest["p_hpa"],
                               latest["rate_mmh"], latest["accum_mm"], prev_accum)
    if dry_run:
        log(f"[dry run] gap rows: {len(gap_rows)}; latest: {row}")
        return False

    dataset = dataset_getter()
    if gap_rows:
        upload_rows_file(dataset, gap_rows)
        log(f"Gap filled: {len(gap_rows)} reading(s) {gap_rows[0]['timestamp']} -> {gap_rows[-1]['timestamp']}")
    stream_rows(dataset, [row])
    state.update({"last_ts": row["timestamp"], "last_accum_mm": prev_accum})
    save_state(state)
    log(f"Sent {row['timestamp']}  T={row['L62_temp_F']}F  wind={row['L62_wind_speed_kt']}kt "
        f"@{row['L62_wind_dir_deg']}  alt={row['L62_altimeter_inHg']}inHg")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--set-token", action="store_true")
    ap.add_argument("--loop", action="store_true",
                    help="keep running, checking every minute, for L62_LOOP_MINUTES (cloud mode)")
    args = ap.parse_args()
    if args.set_token:
        sys.exit(set_token())

    state = load_state()
    if not args.dry_run and not args.loop:
        ensure_task_interval(state)
        save_state(state)

    cache = {}

    def dataset_getter():
        if "ds" not in cache:
            cache["ds"] = get_dataset()
        return cache["ds"]

    if not args.loop:
        run_once(state, dataset_getter, args.dry_run)
        return

    import time
    minutes = float(os.environ.get("L62_LOOP_MINUTES", "60"))
    end = time.time() + minutes * 60
    log(f"Cloud loop started for {minutes:.0f} min (last sent {state['last_ts']}).")
    failures = 0
    while time.time() < end:
        started = time.time()
        try:
            run_once(state, dataset_getter, args.dry_run)
            failures = 0
        except Exception as e:
            failures += 1
            cache.clear()
            log(f"ERROR (attempt {failures}): {type(e).__name__}: {e}")
            if failures >= 10:
                raise
        time.sleep(max(5.0, 60.0 - (time.time() - started)))
    log("Cloud loop finished.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(1)
