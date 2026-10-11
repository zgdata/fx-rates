#!/usr/bin/env python3
"""Exit 0 if rates.json content changed vs. git HEAD (ignoring timestamps/timings), else 1."""
import json, subprocess, sys

VOLATILE = {"generated_at", "finished_at", "fetched_at", "elapsed_ms", "last_attempt_at"}


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [strip(x) for x in o]
    return o


path = sys.argv[1] if len(sys.argv) > 1 else "rates.json"
new = strip(json.load(open(path, encoding="utf-8")))
try:
    old = strip(json.loads(subprocess.check_output(["git", "show", f"HEAD:{path}"], stderr=subprocess.DEVNULL)))
except Exception:
    sys.exit(0)  # no previous version -> changed
sys.exit(0 if new != old else 1)
