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
  agent posts a thesis back (POST /api/thesis)
  human decides
```

This is the design center: quant signals + agent qualitative analysis + human
judgment = solidified decisions. The tracker is the cockpit, not the pilot.

### MCP / Discord agents

`mcp_server.py` exposes this deliberation loop as a local stdio MCP server.
When registered in Hermes, its tools are automatically available to the
configured Discord-agent profiles: `get_portfolio_summary`, `get_config`,
`get_analyst_detail`, and `post_thesis`. The tools deliberately support reading
signals and recording an agent thesis, **not** adding, removing, or trading
positions.

If the Conviction HTTP app uses Basic Auth, configure the MCP process with a
private `CONVICTION_AUTH_FILE` that contains `CONVICTION_AUTH_USERNAME` and
`CONVICTION_AUTH_PASSWORD`. Keep that file outside the repository and Hermes
configuration; pass only its filesystem path in the MCP server's `env` mapping.

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
launchd from reading it. Keep any public-deployment environment file outside the
repository too. For example:

```bash
mkdir -p ~/.config/conviction
chmod 700 ~/.config/conviction
cp .env.public.example ~/.config/conviction/public.env
chmod 600 ~/.config/conviction/public.env
```

Replace the password placeholder in that private file, then have the runner script
load it before starting Conviction (for example, with `set -a; .
~/.config/conviction/public.env; set +a`). Do not put credentials in the
LaunchAgent plist, shell history, or this repository.

### Remote access

Private access is the default: use Conviction locally, on a private LAN, or through
[Tailscale](https://tailscale.com)/WireGuard. For public access, place it only
behind a TLS-terminating reverse proxy or a Cloudflare Tunnel. Keep Conviction
bound to localhost (`HOST=127.0.0.1`) so it is reachable only by that proxy/tunnel,
not directly from the network.

For an authenticated public deployment, copy `.env.public.example` to a private
location such as `~/.config/conviction/public.env`, set a long unique
`CONVICTION_AUTH_PASSWORD`, and load it in the process that starts Conviction.
Set `CONVICTION_AUTH_ENABLED=1`; `CONVICTION_AUTH_USERNAME` selects the Basic Auth
username. Basic Auth is opt-in: when enabled, browsers display their native Basic
Authentication prompt before serving the app or its API. This documentation does
not imply that any public deployment has been made.

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

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Listening port. |
| `HOST` | `127.0.0.1` | Listening address; use `127.0.0.1` for a reverse-proxy or Cloudflare Tunnel deployment. |
| `PORTFOLIO_DATA_DIR` | app folder | Directory for `config.json`, the price DB, and cache. |
| `CONVICTION_AUTH_ENABLED` | unset/disabled | Set to `1` to enable HTTP Basic Authentication. |
| `CONVICTION_AUTH_USERNAME` | — | Basic Auth username when authentication is enabled. |
| `CONVICTION_AUTH_PASSWORD` | — | Basic Auth password when authentication is enabled; keep it only in a private environment file. |

See `.env.public.example` for a non-secret public-deployment template. Never commit
a real password or place one in `config.json`.

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
- **Access control and exposure.** Private/Tailscale access is the default. Basic
  Auth is opt-in via `CONVICTION_AUTH_ENABLED=1` plus private username/password
  environment variables; it is not a substitute for TLS. A public deployment must
  use a TLS reverse proxy or Cloudflare Tunnel and bind Conviction to localhost.
  Never commit credentials or store them in `config.json`.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines, scope guardrails, and the
Developer Certificate of Origin (DCO) process.

---

## License

[GNU AGPL-3.0](LICENSE) — Copyright (C) 2026 Thorsten Jelinek.
