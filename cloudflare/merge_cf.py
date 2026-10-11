"""Merge Cloudflare Worker box_sources.json with the repo's box_sources.json: per source, keep the
entry with the newer fetched_at among 'ok' entries (fall back to whichever has rates)."""
import json, sys, datetime as dt
def load(p):
    try:
        return json.load(open(p, encoding="utf-8")).get("sources", {})
    except Exception:
        return {}
def ts(e):
    try: return dt.datetime.fromisoformat(e["fetched_at"].replace("Z", "+00:00")).timestamp()
    except Exception: return 0
def rank(e):
    return (bool(e and e.get("rates")), e.get("status") == "ok" if e else False, ts(e) if e else 0)
box, cf, out = load(sys.argv[1]), load(sys.argv[2]), sys.argv[3]
merged = {k: max((x for x in (box.get(k), cf.get(k)) if x), key=rank) for k in set(box) | set(cf)}
json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           "fetched_on": "merged(box,cloudflare)", "sources": merged}, open(out, "w", encoding="utf-8"), ensure_ascii=False)
for k, e in sorted(merged.items()):
    print(f"{k}: {e.get('fetched_via', 'box')} {e.get('status')} {e.get('fetched_at')}")
