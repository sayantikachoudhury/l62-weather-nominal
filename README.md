# L62 weather → Nominal (live, every minute)

Streams observed weather (no forecast) from the WeatherXM station **high-indigo-air** at Elk Hills–Buttonwillow Airport (L62) into a Nominal dataset, once a minute. 

## How it works

- `.github/workflows/l62-weather.yml` starts a job every hour. Each job checks the station once a minute for about 62 minutes, and jobs queue back-to-back.
- `l62_weather_sync.py --loop` does the checking:
  - It reads the station's latest reading. WeatherXM publishes a new one every minute.
  - It converts the units: °F, kt, inHg, inches.
  - It computes an altimeter setting for the 326 ft field elevation.
  - It streams the reading to Nominal.
- If a job starts late, the script fills the missing time from the 15-minute history at <https://cole581.github.io/ButtonwillowWeather/>.
- `l62_sync_state.json` stores the last reading sent. The workflow commits it after each job, so nothing is sent twice.

## Channels

| Channel | Meaning |
|---|---|
| `L62_temp_F`, `L62_dewpoint_F` | Temperature and dew point, °F |
| `L62_humidity_pct` | Relative humidity, % |
| `L62_wind_speed_kt`, `L62_wind_gust_kt` | Wind speed and gust, kt |
| `L62_wind_dir_deg` | Direction the wind blows FROM, ° clockwise from TRUE north |
| `L62_pressure_hPa`, `L62_pressure_inHg` | Station (absolute) pressure |
| `L62_altimeter_inHg` | Computed altimeter setting. Unofficial: use AWOS/ATIS for operations |
| `L62_precip_rate_in_per_hr`, `L62_precip_since_last_in`, `L62_precip_accum_in` | Precipitation |

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `NOMINAL_TOKEN` | Nominal API key |
| `NOMINAL_DATASET_RID` | Target dataset RID |
| `NOMINAL_WORKSPACE_RID` | Nominal workspace RID |

GitHub stores these encrypted and masks them in logs.

## Start / stop

- **Actions tab → "L62 weather to Nominal" → Run workflow** starts it immediately.
- **Actions tab → "…" → Disable workflow** stops it.

Note: GitHub pauses scheduled workflows in public repos after 60 days with no repository activity. The hourly state commits count as activity.
