// L62 weather -> Nominal, every minute (Cloudflare Worker with a "* * * * *" cron trigger).
// Reads the latest observation from the WeatherXM station "high-indigo-air" at L62 and writes it to
// the Nominal dataset as 13 channels (same names as the existing L62 Weather tab).
// Secrets/vars (Worker > Settings > Variables and Secrets):
//   NOMINAL_TOKEN        (secret)  Nominal API key
//   NOMINAL_DATASET_RID  (text)    target dataset RID
//   NOMINAL_BASE_URL     (text, optional) default https://api.gov.nominal.io/api
// Opening the Worker URL in a browser shows the latest station reading (read-only, sends nothing).

const WXM_URL =
  "https://api.weatherxm.com/api/v1/cells/8729ac626ffffff/devices/a1e49090-ad73-11ef-8b2f-f594437d6709";
const FIELD_ELEV_M = 326 * 0.3048; // L62 field elevation
const STATE_KEY = "https://l62-state.internal/last"; // Cache API key used to avoid re-sending a reading

const r = (v, nd) => (v == null || !isFinite(v) ? null : Math.round(v * 10 ** nd) / 10 ** nd);

function altimeterHpa(p) {
  if (p == null) return null;
  const n = 0.190284, q = p - 0.3;
  return q * Math.pow(1 + ((Math.pow(1013.25, n) * 0.0065) / 288.15) * (FIELD_ELEV_M / Math.pow(q, n)), 1 / n);
}

async function fetchLatest() {
  const res = await fetch(WXM_URL, { headers: { "cache-control": "no-cache" }, cf: { cacheTtl: 0 } });
  if (!res.ok) throw new Error(`WeatherXM HTTP ${res.status}`);
  const cw = (await res.json()).current_weather;
  if (!cw || !cw.timestamp) throw new Error("WeatherXM: no current_weather");
  return cw;
}

function toChannels(cw, prevAccum) {
  const num = (x) => (x == null || x === "" || isNaN(+x) ? null : +x);
  const t = num(cw.temperature), d = num(cw.dew_point), w = num(cw.wind_speed), g = num(cw.wind_gust);
  const p = num(cw.pressure), rate = num(cw.precipitation), acc = num(cw.precipitation_accumulated);
  let inc = null;
  if (acc != null && prevAccum != null) inc = acc >= prevAccum ? acc - prevAccum : acc;
  const alt = altimeterHpa(p);
  return {
    L62_temp_F: r(t == null ? null : (t * 9) / 5 + 32, 2),
    L62_dewpoint_F: r(d == null ? null : (d * 9) / 5 + 32, 2),
    L62_humidity_pct: r(num(cw.humidity), 1),
    L62_wind_speed_kt: r(w == null ? null : w * 1.943844, 2),
    L62_wind_gust_kt: r(g == null ? null : g * 1.943844, 2),
    L62_wind_dir_deg: r(num(cw.wind_direction), 0),
    L62_pressure_hPa: r(p, 1),
    L62_pressure_inHg: r(p == null ? null : p * 0.02953, 2),
    L62_altimeter_inHg: r(alt == null ? null : alt * 0.02953, 2),
    L62_precip_rate_in_per_hr: r(rate == null ? null : rate / 25.4, 3),
    L62_precip_since_last_in: r(inc == null ? null : inc / 25.4, 3),
    L62_precip_accum_in: r(acc == null ? null : acc / 25.4, 3),
  };
}

async function writeToNominal(env, tsIso, channels) {
  const ms = Date.parse(tsIso);
  const timestamp = { seconds: Math.floor(ms / 1000), nanos: (ms % 1000) * 1e6 };
  const batches = Object.entries(channels)
    .filter(([, v]) => v != null)
    .map(([channel, value]) => ({ channel, tags: {}, points: { type: "double", double: [{ timestamp, value }] } }));
  const base = (env.NOMINAL_BASE_URL || "https://api.gov.nominal.io/api").replace(/\/$/, "");
  const res = await fetch(`${base}/storage/writer/v1`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
      authorization: `Bearer ${String(env.NOMINAL_TOKEN || "").trim()}`,
    },
    body: JSON.stringify({ batches, dataSourceRid: env.NOMINAL_DATASET_RID }),
  });
  if (!res.ok) throw new Error(`Nominal HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return batches.length;
}

async function getState() {
  try {
    const m = await caches.default.match(STATE_KEY);
    return m ? await m.json() : {};
  } catch { return {}; }
}
async function putState(s) {
  try {
    await caches.default.put(STATE_KEY, new Response(JSON.stringify(s), {
      headers: { "content-type": "application/json", "cache-control": "max-age=86400" },
    }));
  } catch {}
}

async function runOnce(env) {
  if (!env.NOMINAL_TOKEN || !env.NOMINAL_DATASET_RID) throw new Error("NOMINAL_TOKEN / NOMINAL_DATASET_RID not set");
  const cw = await fetchLatest();
  const ts = new Date(cw.timestamp).toISOString();
  const state = await getState();
  if (state.last_ts && ts <= state.last_ts) return { sent: false, reason: "already sent", ts };
  const channels = toChannels(cw, state.last_accum_mm ?? null);
  const n = await writeToNominal(env, ts, channels);
  await putState({ last_ts: ts, last_accum_mm: cw.precipitation_accumulated ?? state.last_accum_mm ?? null });
  return { sent: true, ts, channels: n, temp_F: channels.L62_temp_F, alt_inHg: channels.L62_altimeter_inHg };
}

export default {
  // Runs every minute (cron trigger "* * * * *").
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      runOnce(env).then(
        (res) => console.log(JSON.stringify(res)),
        (err) => console.error(`L62 sync error: ${err.message}`)
      )
    );
  },
  // Visiting the Worker URL shows the latest station reading (read-only).
  async fetch(request, env) {
    try {
      const cw = await fetchLatest();
      const body = { station: "high-indigo-air (L62)", latest: cw.timestamp, preview: toChannels(cw, null),
        configured: Boolean(env.NOMINAL_TOKEN && env.NOMINAL_DATASET_RID), state: await getState() };
      return new Response(JSON.stringify(body, null, 2), { headers: { "content-type": "application/json" } });
    } catch (e) {
      return new Response(`Error: ${e.message}`, { status: 502 });
    }
  },
};
