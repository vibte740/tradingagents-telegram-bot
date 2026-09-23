#!/usr/bin/env python3
"""TradingAgents Telegram bot: ticker in, analysis result out.

Send a ticker (e.g. ``BTC-USD`` or ``NVDA``) and the bot runs a full
TradingAgents analysis with the repo's .env config (LLM provider, models,
backend) and replies with the signal + portfolio decision. Results are also
appended to RESEARCH_NOTES.md and the Notion results DB via
scripts/log_research_to_notion.py.

Commands:
    <TICKER> [YYYY-MM-DD]   analyze (date defaults to today)
    /analyze <TICKER> [DATE]
    /status                 queue length / worker state
    /help

Env (via .env / compose env_file):
    TRADINGAGENTS_TG_BOT_TOKEN   dedicated BotFather token (required; must NOT
                                 be shared with another polling bot)
    TRADINGAGENTS_TG_ALLOWED_IDS comma-separated Telegram user/chat IDs
                                 (required, fail-closed: empty = idle)
    TRADINGAGENTS_TG_POLL_SECONDS long-poll interval, default 2

One analysis runs at a time (single worker queue); extra requests wait.
A run takes several minutes — the bot acks immediately, then replies.
Stdlib only (urllib), mirroring trading-gateway/src/telegram/bot.ts.
"""

from __future__ import annotations

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    from tradingagents.dataflows.symbol_utils import normalize_symbol
except Exception:  # host without deps: plain upper-case fallback
    def normalize_symbol(s: str) -> str:
        return s.strip().upper()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TOKEN = os.environ.get("TRADINGAGENTS_TG_BOT_TOKEN", "").strip()
ALLOWED = {
    s.strip()
    for s in os.environ.get("TRADINGAGENTS_TG_ALLOWED_IDS", "").split(",")
    if s.strip()
}
POLL_SECONDS = float(os.environ.get("TRADINGAGENTS_TG_POLL_SECONDS", "2") or 2)

API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")
VALID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-^="
)

HELP_TEXT = (
    "📈 TradingAgents bot\n\n"
    "Send a ticker to analyze it, e.g. `BTC-USD` or `NVDA [YYYY-MM-DD]`.\n"
    "Date defaults to today.\n\n"
    "/analyze <TICKER> [DATE] — same thing\n"
    "/status — queue / worker state\n"
    "/help — this text"
)

MAP_TO_NOTION = {
    "Buy": "Buy",
    "Overweight": "Buy",
    "Hold": "Hold",
    "Underweight": "Sell",
    "Sell": "Sell",
    "REVIEW": "Hold",
}


# ---------- Telegram API (stdlib) ----------

def tg(method: str, params: dict, timeout: int = 40) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send_message(chat_id: int, text: str) -> None:
    for i in range(0, max(len(text), 1), 4000):
        chunk = text[i : i + 4000] or "…"
        tg("sendMessage", {"chat_id": chat_id, "text": chunk})


def get_updates(offset: int | None) -> tuple[list, int | None]:
    params = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        res = tg("getUpdates", params, timeout=35)
    except Exception:
        return [], offset
    updates = res.get("result", []) if res.get("ok") else []
    nxt = offset
    for u in updates:
        nxt = u["update_id"] + 1
    return updates, nxt


# ---------- Parsing ----------

def valid_ticker(value: str) -> bool:
    v = value.strip()
    return bool(v) and all(c in VALID_CHARS for c in v) and len(v) <= 32


def valid_date(value: str) -> str | None:
    try:
        d = datetime.datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    if d > datetime.date.today():
        return None
    return d.isoformat()


def parse_message(text: str):
    """Return (kind, payload...). kinds: help/status/analyze/error."""
    t = (text or "").strip()
    if t.lower() in ("/start", "/help", "help"):
        return ("help",)
    if t.lower() == "/status":
        return ("status",)
    if t.lower().startswith("/analyze"):
        t = t[len("/analyze") :].strip()
    parts = t.split()
    if not parts:
        return ("error", "Send a ticker, e.g. BTC-USD or NVDA.")
    if not valid_ticker(parts[0]):
        return ("error", f"Invalid ticker {parts[0]!r}. Use e.g. BTC-USD, NVDA.")
    ticker = normalize_symbol(parts[0])
    date = datetime.date.today().isoformat()
    if len(parts) > 2:
        return ("error", "Too many arguments. Format: TICKER [YYYY-MM-DD].")
    if len(parts) > 1:
        d = valid_date(parts[1])
        if d is None:
            return ("error", f"Invalid date {parts[1]!r}. Use YYYY-MM-DD, not in the future.")
        date = d
    return ("analyze", ticker, date)


def is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith(CRYPTO_SUFFIXES)


# ---------- Analysis (heavy imports lazy) ----------

OPENROUTER_URL = "https://openrouter.ai/api/v1"


def primary_up(backend_url: str, api_key: str) -> bool:
    """Probe the primary OpenAI-compatible backend; False = use fallback."""
    try:
        url = backend_url.rstrip("/") + "/models"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        return bool(d.get("data"))
    except Exception:
        return False


def resolve_llm(prefer_secondary: bool = False) -> tuple[str, str, str, str, bool]:
    """Return (provider, backend_url, deep, quick, via_secondary).

    Primary is OpenRouter (normalizes every provider to clean OpenAI
    tool-call format). Secondary is the configured TRADINGAGENTS_* backend
    (9router combos can return non-standard shapes). Raises RuntimeError
    if neither is usable.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    if not prefer_secondary:
        or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        pr_deep = os.environ.get("TRADINGAGENTS_TG_PRIMARY_DEEP", "").strip()
        pr_quick = os.environ.get("TRADINGAGENTS_TG_PRIMARY_QUICK", "").strip()
        if or_key and pr_deep and pr_quick and primary_up(OPENROUTER_URL, or_key):
            return "openrouter", OPENROUTER_URL, pr_deep, pr_quick, False
    provider = str(DEFAULT_CONFIG.get("llm_provider", "openai_compatible"))
    backend = str(DEFAULT_CONFIG.get("backend_url") or "")
    deep = str(DEFAULT_CONFIG.get("deep_think_llm", ""))
    quick = str(DEFAULT_CONFIG.get("quick_think_llm", ""))
    if backend:
        key = os.environ.get("OPENAI_COMPATIBLE_API_KEY", "")
        if primary_up(backend, key):
            return provider, backend, deep, quick, True
    raise RuntimeError("no LLM backend reachable (OpenRouter and 9router both down)")


def run_analysis(ticker: str, date: str, prefer_secondary: bool = False) -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.reporting import write_report_tree
    from pathlib import Path

    provider, backend, deep, quick, via_secondary = resolve_llm(prefer_secondary)
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = provider
    config["backend_url"] = backend or None
    config["deep_think_llm"] = deep
    config["quick_think_llm"] = quick
    analysts = ["market", "social", "news", "fundamentals"]
    asset = "crypto" if is_crypto(ticker) else "stock"
    if asset == "crypto":
        analysts = [a for a in analysts if a != "fundamentals"]
    graph = TradingAgentsGraph(analysts, config=config, debug=False)
    final_state, signal = graph.propagate(ticker, date, asset_type=asset)
    results_dir = Path(config["results_dir"]) / ticker / date
    report_path = write_report_tree(final_state, ticker, results_dir)
    risk = final_state.get("risk_debate_state") or {}
    decision_text = (
        risk.get("judge_decision")
        or final_state.get("trader_investment_plan")
        or "(no decision text)"
    )
    return {
        "ticker": ticker,
        "date": date,
        "signal": str(signal),
        "decision_text": str(decision_text),
        "report_path": str(report_path),
        "provider": str(config.get("llm_provider", "")),
        "analysts": ",".join(analysts),
        "via_secondary": via_secondary,
        "models": f"{deep}/{quick}",
    }


def log_result(res: dict) -> None:
    summary = " ".join(res["decision_text"].split())[:300]
    script = os.path.join(REPO_ROOT, "scripts", "log_research_to_notion.py")
    try:
        subprocess.run(
            [
                sys.executable,
                script,
                "--ticker", res["ticker"],
                "--date", res["date"],
                "--decision", MAP_TO_NOTION.get(res["signal"], "Hold"),
                "--summary", f"[{res['signal']}] {summary}",
                "--provider", f"telegram-bot:{res['provider']}{':9router-secondary' if res['via_secondary'] else ''}:{res['models']}",
                "--analysts", res["analysts"],
                "--report-path", res["report_path"],
            ],
            check=False,
            timeout=120,
        )
    except Exception as e:
        print(f"logging failed (non-fatal): {e}", flush=True)


# ---------- Worker ----------

CREDIT_ERROR_HINTS = ("402", "429", "more credits", "rate limit", "rate-limit")


def _is_credit_error(e: Exception) -> bool:
    text = str(e).lower()
    return any(h in text for h in CREDIT_ERROR_HINTS)


work_q: queue.Queue = queue.Queue()
worker_busy = threading.Event()


def worker() -> None:
    while True:
        chat_id, ticker, date = work_q.get()
        worker_busy.set()
        try:
            send_message(chat_id, f"▶️ Starting {ticker} ({date})…")
            try:
                res = run_analysis(ticker, date)
            except Exception as e:
                if not _is_credit_error(e):
                    raise
                send_message(
                    chat_id,
                    "⛽ OpenRouter is out of credits/rate-limited "
                    "(top up at openrouter.ai/settings/credits) — "
                    "retrying via 9router secondary…",
                )
                res = run_analysis(ticker, date, prefer_secondary=True)
            excerpt = res["decision_text"]
            if len(excerpt) > 3200:
                excerpt = excerpt[:3200] + "\n…(truncated, full report on disk)"
            route = (
                f"via 9router secondary ({res['models']}) — OpenRouter was down"
                if res["via_secondary"]
                else f"via OpenRouter ({res['models']})"
            )
            send_message(
                chat_id,
                f"📊 {res['ticker']} · {res['date']}\n"
                f"Signal: {res['signal']}\n{route}\n\n{excerpt}",
            )
            log_result(res)
        except Exception as e:
            import traceback

            traceback.print_exc()
            try:
                send_message(chat_id, f"⚠️ Analysis of {ticker} failed: {str(e)[:400]}")
            except Exception:
                pass
        finally:
            if work_q.empty():
                worker_busy.clear()


# ---------- Poll loop ----------

def handle_text(chat_id: int, text: str) -> None:
    parsed = parse_message(text)
    kind = parsed[0]
    if kind == "help":
        send_message(chat_id, HELP_TEXT)
    elif kind == "status":
        state = "busy" if worker_busy.is_set() else "idle"
        send_message(chat_id, f"Worker: {state}, queued: {work_q.qsize()}")
    elif kind == "error":
        send_message(chat_id, f"⚠️ {parsed[1]}")
    elif kind == "analyze":
        _, ticker, date = parsed
        pos = work_q.qsize() + (1 if worker_busy.is_set() else 0)
        work_q.put((chat_id, ticker, date))
        extra = f" (queue #{pos + 1})" if pos else ""
        send_message(chat_id, f"🔍 Analyzing {ticker} on {date}…{extra}\nThis takes several minutes — I'll reply here.")


def main() -> None:
    if not TOKEN or not ALLOWED:
        print(
            "telegram-bot idle: set TRADINGAGENTS_TG_BOT_TOKEN and "
            "TRADINGAGENTS_TG_ALLOWED_IDS (fail-closed)",
            flush=True,
        )
        while True:
            time.sleep(3600)
    print("telegram-bot polling…", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    offset: int | None = None
    backoff = 1
    while True:
        try:
            updates, offset = get_updates(offset)
            backoff = 1
            for u in updates:
                msg = u.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text", "")
                if chat_id is None or not text:
                    continue
                if str(chat_id) not in ALLOWED:
                    try:
                        send_message(chat_id, "⛔ This bot is private.")
                    except Exception:
                        pass
                    continue
                try:
                    handle_text(chat_id, text)
                except Exception as e:
                    try:
                        send_message(chat_id, f"⚠️ {str(e)[:400]}")
                    except Exception:
                        pass
        except Exception as e:
            print(f"poll error: {e}", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
