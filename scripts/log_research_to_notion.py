#!/usr/bin/env python3
"""Log a TradingAgents result to RESEARCH_NOTES.md and the Notion results DB.

Usage:
    python3 scripts/log_research_to_notion.py --ticker BTC-USD --date 2026-09-23 \\
        --decision Hold --summary "one-line thesis" [--provider openai ...] [--no-notion]

Auth via NOTION_API_KEY env var. Never pass secrets as CLI args.
Stdlib only.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.request

DB_ID = "3e4239d8-18ee-8139-894b-fcee476e68a1"
NOTE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "RESEARCH_NOTES.md")


def notion_create_row(api_key, props):
    body = {"parent": {"database_id": DB_ID}, "properties": props}
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["id"], r.status


def append_note(ticker, date, decision, summary):
    line = f"| {date} | {ticker} | {decision} | {summary} |\n"
    with open(NOTE_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    return line


def main():
    p = argparse.ArgumentParser(description="Log a TradingAgents research result.")
    p.add_argument("--ticker", required=True)
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.add_argument("--decision", required=True, choices=["Buy", "Sell", "Hold"])
    p.add_argument("--summary", required=True)
    p.add_argument("--provider", default="")
    p.add_argument("--analysts", default="")
    p.add_argument("--report-path", default="")
    p.add_argument("--no-notion", action="store_true")
    a = p.parse_args()

    line = append_note(a.ticker, a.date, a.decision, a.summary)
    print(f"note-updated: {line.strip()}")

    if a.no_notion:
        print("notion: skipped (--no-notion)")
        return
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        print("notion: missing NOTION_API_KEY, note file only", file=sys.stderr)
        sys.exit(2)
    props = {
        "Name": {"title": [{"text": {"content": f"{a.ticker} · {a.date}"}}]},
        "Ticker": {"rich_text": [{"text": {"content": a.ticker}}]},
        "Analysis date": {"date": {"start": a.date}},
        "Decision": {"select": {"name": a.decision}},
        "Summary": {"rich_text": [{"text": {"content": a.summary[:1900]}}]},
        "Provider": {"rich_text": [{"text": {"content": a.provider}}]},
        "Analysts": {"rich_text": [{"text": {"content": a.analysts}}]},
        "Report path": {"rich_text": [{"text": {"content": a.report_path}}]},
    }
    page_id, status = notion_create_row(api_key, props)
    print(f"notion-row: {page_id} ({status})")


if __name__ == "__main__":
    main()
