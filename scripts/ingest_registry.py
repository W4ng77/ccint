"""把 ransomware.live 的受害者记录灌进 external_events。"""
import argparse, datetime as dt, logging, sys, time
from ccint.registry import ransomwarelive as rl

logging.basicConfig(level=logging.WARNING, format="%(message)s")
ap = argparse.ArgumentParser()
ap.add_argument("--countries", default="CA,US,FR,GB")
ap.add_argument("--since", default="2026-07-01")
ap.add_argument("--until", default="2026-09-22")
a = ap.parse_args()
since, until = dt.date.fromisoformat(a.since), dt.date.fromisoformat(a.until)
for i, c in enumerate(a.countries.split(",")):
    if i: time.sleep(30)                      # API 速率限制
    try:
        print(rl.ingest(c, since=since, until=until), flush=True)
    except RuntimeError as e:
        print(f"!! {c}: {e}", file=sys.stderr, flush=True)
