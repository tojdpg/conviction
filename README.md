# Portfolio Tracker

Self-hosted stock portfolio dashboard: live prices, analyst targets, technical
buy/sell signals, and a watchlist — all in EUR, in a single dark-mode page.
One Python backend file, one HTML file, no database server, no accounts, no cloud.

> UI language is **German** (built for a German broker depot). Data comes from
> Yahoo Finance via [yfinance](https://github.com/ranaroussi/yfinance).

## Features

- **Live prices** for stocks, ETFs, ETCs, and crypto, auto-converted to EUR
  (USD/GBP/GBp/JPY/CHF, London pence handled correctly)
- **Analyst price targets** (mean/low/high + upside) per position
- **ETF-implied targets**: for ETFs without analyst coverage, a weighted target
  is computed from the fund's live top-10 holdings
- **Technical signals** with plain-language buy rules in tooltips:
  RSI 14, SMA 50/200 with Golden-Cross traffic light, distance to all-time high
- **Fundamentals**: P/E (KGV), P/B (KBV)
- **Performance bars** for 1 day up to 48 months, portfolio-wide
- **Sparklines** (3M, 12M with SMA overlay, 5Y) with hover zoom
- **Watchlist** — same signals, no position size
- **Add/remove positions in the UI** — persisted to `config.json`
- **Rebalancing tab** with a simple allocation suggestion
- **Robust data layer**: full price history in a local SQLite store (charts keep
  working when Yahoo rate-limits), stale-while-revalidate caching, batched
  downloads, capped concurrency with exponential backoff

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/YOURNAME/portfoliotracker.git
cd portfoliotracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

Open <http://localhost:8080>. On first start a `config.json` is created from
`config.example.json` with a small demo portfolio — replace it with your own
positions, either in the UI (**＋ Position** button) or by editing the file.

The first start downloads full price history for all tickers; give it a minute.

## Configuration (`config.json`)

```jsonc
{
  "portfolio": [
    // shares = how many you own; currency = trading currency of the ticker
    {"name": "Apple", "ticker": "AAPL", "shares": 10, "currency": "USD"}
  ],
  "watchlist": [
    // same, but without shares
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

## API

| Endpoint | Description |
|---|---|
| `GET /api/portfolio-lite` | Prices + FX only (fast) |
| `GET /api/portfolio` | Everything: signals, analysts, watchlist, performance |
| `GET /api/history/{ticker}?period=5y` | Price history from the local store |
| `GET /api/analysts/{ticker}` | Analyst detail for one instrument |
| `POST /api/positions` | Add position `{ticker, shares?, name?}` (no shares → watchlist) |
| `DELETE /api/positions/{list}/{ticker}` | Remove from `portfolio` or `watchlist` |

## Running permanently

**Docker**

```bash
docker build -t portfoliotracker .
docker run -d -p 8080:8080 -v ./data:/data --name depot portfoliotracker
```

**Linux (systemd)** — `/etc/systemd/system/portfoliotracker.service`:

```ini
[Unit]
Description=Portfolio Tracker
After=network-online.target

[Service]
WorkingDirectory=/opt/portfoliotracker
ExecStart=/opt/portfoliotracker/.venv/bin/python main.py
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

**macOS (launchd)** — create a LaunchAgent that runs a small runner script.
Note: put the runner script *outside* `~/Documents` (e.g. `~/.local/bin/`),
otherwise macOS TCC blocks launchd from reading it.

**Remote access**: the app has **no authentication** — don't expose it to the
internet. Use it on your LAN, or via [Tailscale](https://tailscale.com)/WireGuard
from your phone.

## Caveats

- yfinance is an unofficial scraper of Yahoo Finance; Yahoo occasionally
  rate-limits (HTTP 429). The tracker retries with backoff and serves cached
  data meanwhile, but analyst fields can be temporarily empty.
- The BTC and gold targets in `manual_targets` are hand-maintained opinions
  with an `as_of` date — update or delete them.
- **Not financial advice.** The "Kaufregeln" in the tooltips are simplistic
  heuristics for orientation, not recommendations.

## License

[MIT](LICENSE)
