// fx-box-sources: fetches bank rate pages that block GitHub runners (VietinBank VN, ABA KH,
// Bank of Ceylon LK) on a cron, normalises them to the box_sources.json schema used by
// zgdata/fx-rates, keeps the latest document in Workers KV and serves GET /box_sources.json.
const UA = "Mozilla/5.0 (compatible; TravelFXFetcher/1.0; personal currency app, 1 request/day)";
const KV_KEY = "box_sources.json";
const MAX_AGE_H = 36;

// ---------------------------------------------------------------- helpers (mirror fetch_rates.py)
const ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0" };
function unescape(s) {
  return String(s).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === "#") {
      const n = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(n) ? String.fromCodePoint(n) : m;
    }
    return ENT[e.toLowerCase()] ?? m;
  });
}
function num(s, fmt = "dot") {
  if (s === null || s === undefined) return null;
  if (typeof s === "number") return s > 0 ? s : null;
  s = unescape(s).replace(/\u00a0/g, " ").trim().replace(/[^\d.,\-]/g, "");
  if (!s || s === "-" || s === "0") return null;
  s = fmt === "comma" ? s.replace(/\./g, "").replace(/,/g, ".") : s.replace(/,/g, "");
  const v = Number(s);
  return Number.isFinite(v) && v > 0 ? v : null;
}
const r8 = (x) => (x === null || x === undefined ? null : Number(Number(x).toPrecision(8)));
function mkRow(unit, middle, tb, ts, cb, cs) {
  const u = unit || 1, f = (v) => (v === null || v === undefined ? null : r8(v / u));
  return { middle: f(middle), transfer: { buy: f(tb), sell: f(ts) }, cash: { buy: f(cb), sell: f(cs) } };
}
function vnRow(cashBuy, transferBuy, sell) {
  const mid = transferBuy && sell ? r8((transferBuy + sell) / 2) : null;
  return mkRow(1, mid, transferBuy, sell, cashBuy, sell);
}
function lrow(unit, middle, tb, ts, cb, cs) {
  if (middle === null || middle === undefined) {
    if (tb && ts) middle = (tb + ts) / 2;
    else if (cb && cs) middle = (cb + cs) / 2;
  }
  return mkRow(unit, middle, tb, ts, cb, cs);
}
const strip = (c) => unescape(c.replace(/<[^>]+>/g, "")).trim();
const cells = (tr) => [...tr.matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi)]
  .map((m) => unescape(m[1].replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim());
const rows = (h) => [...h.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/gi)].map((m) => cells(m[1]));
const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");

async function get(url) {
  const r = await fetch(url, { headers: { "User-Agent": UA, "Accept-Language": "hr,en;q=0.8,vi;q=0.6" },
    signal: AbortSignal.timeout(25000) });
  if (!r.ok) throw new Error(`HTTPError: ${r.status} ${r.statusText} for url: ${url}`);
  return await r.text();
}

// ---------------------------------------------------------------- adapters
async function vietinbank() {
  const h = await get("https://www.vietinbank.vn/ca-nhan/ty-gia-khcn");
  const i = h.indexOf("Chuyển khoản</p>");
  if (i < 0) throw new Error("rate table not found");
  const tb = h.slice(h.lastIndexOf("<table", i), h.indexOf("</table>", i));
  const rates = {};
  for (const m of tb.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g)) {
    const c = [...m[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((x) => strip(x[1]));
    if (c.length >= 4 && /^[A-Z]{3}$/.test(c[0]))
      rates[c[0]] = vnRow(num(c[1], "comma"), num(c[2], "comma"), num(c[3], "comma"));
  }
  const m = h.match(/cập nhật lúc\s*(?:<[^>]+>\s*)*([\d:]{5,8})\s*(?:<[^>]+>\s*)*ngày\s*(?:<[^>]+>\s*)*(\d{2}\/\d{2}\/\d{4})/);
  let pub = null;
  if (m) {
    const t = m[1].length === 8 ? m[1] : m[1] + ":00";
    const [d, mo, y] = m[2].split("/");
    pub = `${y}-${mo}-${d}T${t}+07:00`;
  }
  if (!Object.keys(rates).length) throw new Error("no rows parsed");
  return { published_at: pub, middle_source: "computed_avg_transfer", rates };
}

async function aba() {
  const h = await get("https://www.ababank.com/en/forex-exchange/");
  const tabs = h.match(/<table[\s\S]*?<\/table>/g) || [];
  if (tabs.length < 2) throw new Error("expected 2 tables (SWIFT transfer, notes)");
  const pick = (t) => Object.fromEntries(rows(t).filter((c) => c.length >= 3 && /^[A-Z]{3} \/ KHR/.test(c[0])).map((c) => [c[0].slice(0, 3), c]));
  const tr = pick(tabs[0]), ca = pick(tabs[1]);
  const rates = {};
  for (const cur of [...new Set([...Object.keys(tr), ...Object.keys(ca)])].sort()) {
    const t = tr[cur] || [null, null, null], c = ca[cur] || [null, null, null];
    rates[cur] = lrow(1, null, num(t[1]), num(t[2]), num(c[1]), num(c[2]));
  }
  if (!Object.keys(rates).length) throw new Error("no rates parsed");
  return { middle_source: "computed_avg_transfer", rates };
}

async function boc_lk() {
  const h = await get("https://www.boc.lk/rates-tariff");
  const i = h.indexOf("Telegraphic");
  if (i < 0) throw new Error("Telegraphic table not found");
  const t = h.slice(h.lastIndexOf("<table", i), h.indexOf("</table>", i));
  const rates = {};
  for (const c of rows(t)) {
    if (c.length >= 7 && /^[A-Z]{3}$/.test(c[0])) {
      const v = c.slice(1, 7).map((x) => num(x));
      rates[c[0]] = lrow(1, null, v[4], v[5], v[0], v[1]);
    }
  }
  const m = t.match(/as at:\s*([\d.]+ [\d:]+ [AP]M)/);
  if (!Object.keys(rates).length) throw new Error("no rates parsed");
  return { published_at: m ? m[1] : null, middle_source: "computed_avg_transfer",
    note: "cash = Currency notes, transfer = Telegraphic transfers", rates };
}

const SOURCES = {
  "VN/vietinbank": { id: "vietinbank", name: "VietinBank", kind: "bank", url: "https://www.vietinbank.vn/ca-nhan/ty-gia-khcn", page_url: "https://www.vietinbank.vn/ca-nhan/ty-gia-khcn", fn: vietinbank },
  "KH/aba": { id: "aba", name: "ABA Bank", kind: "bank", url: "https://www.ababank.com/en/forex-exchange/", page_url: "https://www.ababank.com/en/forex-exchange/", fn: aba },
  "LK/boc_lk": { id: "boc_lk", name: "Bank of Ceylon", kind: "bank", url: "https://www.boc.lk/rates-tariff", page_url: "https://www.boc.lk/rates-tariff", fn: boc_lk },
};
// cron (UTC) -> sources. VN/KH (UTC+7): 07:30 + 12:30 local; LK (UTC+5:30): 08:30 + 12:30 local.
const CRON_MAP = {
  "30 0 * * *": ["VN/vietinbank", "KH/aba"],
  "30 5 * * *": ["VN/vietinbank", "KH/aba"],
  "0 3 * * *": ["LK/boc_lk"],
  "0 7 * * *": ["LK/boc_lk"],
};

async function runSource(key, prev, now) {
  const s = SOURCES[key];
  const out = { id: s.id, name: s.name, kind: s.kind, url: s.url, page_url: s.page_url, fetched_at: nowIso() };
  const t0 = Date.now();
  try {
    const res = await s.fn();
    Object.assign(out, res, { status: "ok", count: Object.keys(res.rates).length });
  } catch (e) {
    Object.assign(out, { status: "error", error: `${e.name || "Error"}: ${String(e.message || e).slice(0, 300)}`, rates: {} });
  }
  out.elapsed_ms = Date.now() - t0;
  out.fetched_via = "cloudflare";
  // stale fallback: keep last good data if this fetch failed
  if (out.status === "error" && prev && ["ok", "stale"].includes(prev.status) && prev.rates && Object.keys(prev.rates).length) {
    const st = { ...prev, status: "stale", stale_since: prev.stale_since || now, last_error: out.error, last_attempt_at: out.fetched_at };
    delete st.error;
    for (const k of ["name", "kind", "url", "page_url"]) st[k] = s[k];
    st.checked_at = now;
    return st;
  }
  out.checked_at = now;
  return out;
}

async function refresh(env, keys) {
  const prevDoc = (await env.FX_KV.get(KV_KEY, "json")) || { sources: {} };
  const prev = prevDoc.sources || {};
  const now = nowIso();
  // also fetch anything we have never fetched
  const todo = [...new Set([...keys, ...Object.keys(SOURCES).filter((k) => !prev[k])])];
  const results = await Promise.all(todo.map((k) => runSource(k, prev[k], now)));
  const sources = {};
  for (const k of Object.keys(SOURCES)) if (prev[k]) sources[k] = prev[k];
  todo.forEach((k, i) => (sources[k] = results[i]));
  // age-based stale marking for entries that were not refreshed for a long time
  for (const [k, e] of Object.entries(sources)) {
    const age = (Date.parse(now) - Date.parse(e.fetched_at)) / 3.6e6;
    if (e.status === "ok" && age > MAX_AGE_H) {
      e.status = "stale"; e.stale_since = e.stale_since || now;
      e.last_error = `data is ${Math.round(age)} h old (> ${MAX_AGE_H} h)`;
    }
  }
  const doc = { generated_at: now, fetched_on: "cloudflare", max_age_hours: MAX_AGE_H, sources };
  await env.FX_KV.put(KV_KEY, JSON.stringify(doc));
  return { doc, refreshed: todo.map((k) => `${k}:${sources[k].status}`) };
}

const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS" };

// Hourly replacement for GitHub's unreliable schedule: dispatch daily.yml with all=false
// (only due countries). Needs secret GH_TOKEN (fine-grained, zgdata/fx-rates, Actions RW).
const GH_CRON = "23 * * * *";
async function dispatchGithub(env) {
  const r = await fetch("https://api.github.com/repos/zgdata/fx-rates/actions/workflows/daily.yml/dispatches", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "fx-box-sources-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main", inputs: { all: "false" } }),
  });
  console.log("github dispatch", r.status, r.status === 204 ? "" : (await r.text()).slice(0, 300));
  return r.status;
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { headers: { ...CORS, "Access-Control-Max-Age": "86400" } });
    if (url.pathname === "/box_sources.json" && (req.method === "GET" || req.method === "HEAD")) {
      const body = await env.FX_KV.get(KV_KEY);
      if (!body) return new Response('{"error":"no data yet"}', { status: 503, headers: { ...CORS, "content-type": "application/json" } });
      return new Response(req.method === "HEAD" ? null : body, { headers: { ...CORS, "content-type": "application/json; charset=utf-8", "cache-control": "public, max-age=300" } });
    }
    if (url.pathname === "/refresh" && req.method === "POST") {
      if (!env.REFRESH_KEY || req.headers.get("x-refresh-key") !== env.REFRESH_KEY) return new Response("forbidden", { status: 403 });
      const only = url.searchParams.get("only");
      const keys = only ? only.split(",").filter((k) => SOURCES[k]) : Object.keys(SOURCES);
      const { refreshed } = await refresh(env, keys);
      return new Response(JSON.stringify({ refreshed, colo: req.cf && req.cf.colo }), { headers: { "content-type": "application/json" } });
    }
    if (url.pathname === "/") return new Response("fx-box-sources: GET /box_sources.json\n", { headers: CORS });
    return new Response("not found", { status: 404, headers: CORS });
  },
  async scheduled(event, env, ctx) {
    if (event.cron === GH_CRON) {
      ctx.waitUntil(dispatchGithub(env));
      return;
    }
    const keys = CRON_MAP[event.cron] || Object.keys(SOURCES);
    ctx.waitUntil(refresh(env, keys).then((r) => console.log(event.cron, JSON.stringify(r.refreshed))));
  },
};
