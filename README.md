# tradingagents-telegram-bot

Telegram bridge for [TradingAgents](https://github.com/TauricResearch/TradingAgents):
send a ticker, get a full multi-agent analysis back in chat.

```
you:  BTC-USD
bot:  🔍 Analyzing BTC-USD on 2026-09-23…
bot:  📊 BTC-USD · 2026-09-23
      Signal: Hold
      via openai_compatible (muse-spark/muse-spark)
      <portfolio decision…>
```

## Commands

| Input | Meaning |
|---|---|
| `BTC-USD` / `NVDA` | analyze as of today |
| `BCH-USD 2026-09-23` | analyze for a date |
| `/analyze <TICKER> [DATE]` | same thing |
| `/status` | worker / queue state |
| `/help` | help |

Crypto tickers (`-USD`, `-USDT`, …) automatically skip the fundamentals
analyst. One analysis runs at a time; extras queue.

## Setup (as a TradingAgents compose service)

This code is deployed inside the TradingAgents repo (it imports the
framework). Copy `telegram_bot/` next to it or run the provided image:

```yaml
telegram-bot:
  build: .
  entrypoint: ["python", "-u", "telegram_bot/bot.py"]
  env_file:
    - .env
  volumes:
    - tradingagents_data:/home/appuser/.tradingagents
  restart: unless-stopped
```

```bash
cp .env.example .env   # then fill the Telegram + provider keys
docker compose up -d --build telegram-bot
```

## Env vars

See `.env.example`. Essentials:

- `TRADINGAGENTS_TG_BOT_TOKEN` — dedicated BotFather token. Never reuse a
  token polled by another bot.
- `TRADINGAGENTS_TG_ALLOWED_IDS` — comma-separated user/chat IDs (fail-closed).
- LLM comes from the standard `TRADINGAGENTS_*` config (provider, models,
  backend URL).

## Primary / fallback LLMs

Before each run the bot probes the primary backend (`/v1/models`). If it's
down it switches to **OpenRouter** (`OPENROUTER_API_KEY`):

- `TRADINGAGENTS_TG_FALLBACK_DEEP` (default `nvidia/nemotron-3-ultra-550b-a55b:free`)
- `TRADINGAGENTS_TG_FALLBACK_QUICK` (default `poolside/laguna-s-2.1:free`)

The reply states which route was used, and every run is logged.

## Result logging

Each run is appended to `RESEARCH_NOTES.md` and (unless `--no-notion`) to a
Notion results DB via `scripts/log_research_to_notion.py` (needs
`NOTION_API_KEY`). Stdlib only, no extra dependencies.
