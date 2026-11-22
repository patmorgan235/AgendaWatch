#!/usr/bin/env python3
"""agenda-watch — search local government agendas for key terms and write a daily report.

  python agenda_watch.py run              daily run: check agendas, write reports/latest.html
  python agenda_watch.py run --backfill 90   first run: also look back 90 days
  python agenda_watch.py check            show what each city's source finds (no downloads)
  python agenda_watch.py search "bus rapid transit"   search every agenda already downloaded
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import webbrowser
from pathlib import Path

import yaml

from agendawatch.matcher import compile_terms
from agendawatch.report import text_summary, write_report
from agendawatch.runner import check, run
from agendawatch.store import Store

HERE = Path(__file__).resolve().parent


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = HERE / p
    with open(p, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    settings = cfg.setdefault("settings", {})
    # Resolve relative paths against the config file so cron/Task Scheduler runs work from any folder.
    for key, default in (("database", "data/agenda_watch.db"), ("report_dir", "reports")):
        val = Path(settings.get(key, default))
        settings[key] = str(val if val.is_absolute() else p.parent / val)
    return cfg


def cmd_run(args, cfg) -> int:
    res = run(cfg, backfill_days=args.backfill)
    terms = [t if isinstance(t, str) else t["name"] for t in cfg["terms"]]
    path = write_report(res, terms, cfg["settings"]["report_dir"])
    print()
    print(text_summary(res))
    print(f"\nReport: {path}")
    if args.open:
        webbrowser.open(path.as_uri())
    return 0 if not any(s.error for s in res.statuses) else 2


def cmd_check(args, cfg) -> int:
    bad = 0
    for name, platform, docs, msg in check(cfg):
        status = "OK" if docs else ("ERROR" if msg.startswith("ERROR") else "no agendas in window")
        print(f"\n== {name} ({platform}) — {len(docs)} agenda(s) — {status}")
        if msg:
            print(f"   {msg}")
        for d in docs[:8]:
            print(f"   {d.meeting_date or '????-??-??'}  {d.body[:50]:<50}  {d.url[:90]}")
        if len(docs) > 8:
            print(f"   … and {len(docs) - 8} more")
        bad += status == "ERROR"
    return 2 if bad else 0


def cmd_search(args, cfg) -> int:
    store = Store(cfg["settings"]["database"])
    if args.regex:
        regex = re.compile(args.query, 0 if args.case_sensitive else re.I)
    else:
        regex = compile_terms([{"name": args.query, "case_sensitive": args.case_sensitive}])[0].regex
    results = store.search(regex, city=args.city, limit=args.limit)
    if not results:
        print("No matches in the agendas downloaded so far.")
        return 0
    for row, m in results:
        text = row["text"]
        snip = re.sub(r"\s+", " ", text[max(0, m.start() - 90): m.end() + 90])
        print(f"{row['meeting_date'] or '????-??-??'}  {row['city']} — {row['body']}  (page {row['page']})")
        print(f"   …{snip}…")
        print(f"   {row['url']}")
    print(f"\n{len(results)} match(es){' (limit reached)' if len(results) >= args.limit else ''}")
    store.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml", help="path to config.yaml")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="check agendas and write the daily report")
    p_run.add_argument("--backfill", type=int, metavar="DAYS", help="look back this many days instead of lookback_days")
    p_run.add_argument("--open", action="store_true", help="open the report in your browser when done")

    sub.add_parser("check", help="test each source without downloading agendas")

    p_s = sub.add_parser("search", help="search agendas already downloaded")
    p_s.add_argument("query")
    p_s.add_argument("--regex", action="store_true", help="treat the query as a regular expression")
    p_s.add_argument("--case-sensitive", action="store_true")
    p_s.add_argument("--city", help="limit to one city")
    p_s.add_argument("--limit", type=int, default=100)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    for noisy in ("httpx", "httpcore", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(args.config)
    return {"run": cmd_run, "check": cmd_check, "search": cmd_search}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
