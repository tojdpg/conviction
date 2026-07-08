# conviction

Self-hosted buy/sell decision cockpit for stock portfolios. Live prices, analyst
targets, ETF-implied targets, technical buy/sell signals, sparklines, performance
bars, watchlist — and a compact agent API for multi-agent investment deliberation.

One Python backend file, one HTML file, no database server, no accounts, no cloud.

> **Screenshot:** see [`docs/screenshot.png`](docs/screenshot.png) — captured from
> the bundled demo config, not real portfolio data.

---

## Why

Most portfolio trackers show you *what you have*. conviction shows you *whether to
act*: every position and watchlist item carries technical signals (RSI, SMA 50/200
golden-cross, distance from all-time high), analyst consensus targets with upside %,
and ETF-implied targets for funds without analyst coverage. The quant signals are
deliberately simplistic heuristics — they are orientation, not oracles. The real
deliberation (structural, fundamental, "is the business intact?") is yours — or your
agents'.

### Multi-agent deliberation

The `/api/summary` endpoint produces a compact (~4 KB markdown, ~10 KB JSON) report
of all positions and signals — designed for LLM context windows, not for browsers.
Point your agent at it, let it reason about the signals, and close the loop:

```
GET /api/summary?format=md    → feed to your LLM agent
  agent reasons about signals, fundamentals, context
  agent posts a thesis back (POST /api/thesis — planned)
  human decides
```

This is the design center: quant signals + agent qualitative analysis + human
judgment = solidified decisions. The tracker is the cockpit, not the pilot.

---

## Features

- **Live prices** for stocks, ETFs, ETCs, and crypto, auto-converted to your base
  currency (USD/GBP/GBp/JPY/CHF — London pence handled correctly)
- **Analyst price targets** (mean/low/high + upside) per position
- **ETF-implied targets**: for ETFs without analyst coverage, a weighted target is
  computed from the fund's live top-10 holdings
- **Technical signals** with plain-language buy rules in tooltips:
  RSI 14, SMA 50/200 with Golden-Cross traffic light, distance to all-time high
- **Fundamentals**: P/E, P/B
- **Performance bars** for 1 day up to 48 months, portfolio-wide
- **Sparklines** (3M, 12M with SMA overlay, 5Y) with hover zoom
- **Watchlist** — same signals, no position size
- **Add/remove positions in the UI** — persisted to `config.json`
- **Rebalancing tab** with a simple allocation suggestion
- **Robust data layer**: full price history in a local SQLite store (charts keep
  working when Yahoo rate-limits), stale-while-revalidate caching, batched
  downloads, capped concurrency with exponential backoff
- **Agent-friendly API**: compact summary endpoint for LLM context windows

---

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/tojdpg/conviction.git
cd portfoliotracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

Open <http://localhost:8080>. On first start a `config.json` is created from
`config.example.json` with a small demo portfolio — replace it with your own
positions, either in the UI (**+ Position** button) or by editing the file.

The first start downloads full price history for all tickers; give it a minute.

### Docker

```bash
docker build -t conviction .
docker run -d -p 8080:8080 -v ./data:/data --name conviction conviction
```

### systemd (Linux)

`/etc/systemd/system/conviction.service`:

```ini
[Unit]
Description=conviction portfolio cockpit
After=network-online.target

[Service]
WorkingDirectory=/opt/conviction
ExecStart=/opt/conviction/.venv/bin/python main.py
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

### macOS (launchd)

Create a LaunchAgent that runs a small runner script. **Note:** put the runner
script *outside* `~/Documents` (e.g. `~/.local/bin/`), otherwise macOS TCC blocks
launchd from reading it.

### Remote access

The app has **no authentication** — don't expose it to the internet. Use it on
your LAN, or via [Tailscale](https://tailscale.com)/WireGuard from your phone.

---

## Configuration (`config.json`)

```jsonc
{
  "portfolio": [
    // shares = how many you own; currency = trading currency of the ticker
    // buy_price (optional) = avg cost per share in your base currency -> enables the P/L column
    {"name": "Apple", "ticker": "AAPL", "shares": 10, "currency": "USD", "buy_price": 145.20}
  ],
  "watchlist": [
    // same shape, but without shares
    {"name": "Allianz", "ticker": "ALV.DE", "currency": "EUR"}
  ],
  "manual_targets": { /* price targets for BTC/gold with source + as-of date */ },
  "etf_targets":    { /* static top-holdings fallback per ETF ticker */ }
}
```

Tickers are **Yahoo Finance symbols**:

| Exchange | Format | Example |
|---|---|---|
| NYSE/NASDAQ | `SYMBOL` | `NVDA`, `AAPL` |
| Xetra/Frankfurt | `SYMBOL.DE` | `SAP.DE` |
| Amsterdam | `SYMBOL.AS` | `IWDA.AS` |
| London | `SYMBOL.L` | `HSBA.L` (quoted in pence — handled) |
| Swiss | `SYMBOL.SW` | `EXCH.SW` |
| Madrid / Paris / Milan | `.MC` / `.PA` / `.MI` | `BBVA.MC`, `BNP.PA` |
| Tokyo | `SYMBOL.T` | `7203.T` |
| Crypto | `SYMBOL-USD` | `BTC-USD` |

Environment variables: `PORT` (default 8080), `HOST` (default 0.0.0.0),
`PORTFOLIO_DATA_DIR` (where config/DB/cache live; defaults to the app folder).

---

## API

| Endpoint | Description |
|---|---|
| `GET /api/portfolio-lite` | Prices + FX only (fast) |
| `GET /api/portfolio` | Everything: signals, analysts, watchlist, performance |
| `GET /api/summary?format=md` | Compact agent/LLM-friendly report (markdown or JSON, ~4 KB, no history arrays) |
| `GET /api/history/{ticker}?period=5y` | Price history from the local store |
| `GET /api/analysts/{ticker}` | Analyst detail for one instrument |
| `POST /api/positions` | Add position `{ticker, shares?, name?}` (no shares → watchlist) |
| `DELETE /api/positions/{list}/{ticker}` | Remove from `portfolio` or `watchlist` |

---

## Disclaimers

- **Unofficial API.** Market data is scraped from Yahoo Finance via
  [yfinance](https://github.com/ranaroussi/yfinance). This is not an official
  Yahoo API; data may be delayed, incomplete, or rate-limited (HTTP 429). The
  tracker retries with backoff and serves cached data meanwhile, but analyst
  fields can be temporarily empty.
- **Not financial advice.** The buy rules shown in tooltips are simplistic
  heuristics for orientation — not investment recommendations. A buy signal does
  not mean the business is intact (structural/fundamental judgment is yours).
  Always do your own research.
- **No authentication.** The app has no login. Bind to localhost or use a VPN.
  Don't expose it to the public internet.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines, scope guardrails, and the
Developer Certificate of Origin (DCO) process.

---

## License

[GNU AGPL-3.0](LICENSE) — Copyright (C) 2026 Thorsten Jelinek.
