# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Renamed
- Project renamed from "Flatex Depot Tracker" to **conviction** in all
  documentation. In-app title updated separately. The repo name will follow.

### Added
- English README with feature overview, quickstart, API reference, and
  multi-agent deliberation section highlighting the `/api/summary` endpoint.
- `CONTRIBUTING.md` with scope guardrails (no brokerage integration, no
  transaction import, no auth), DCO sign-off process, and privacy rules.
- Issue templates: bug report, feature request (with scope-check prompt).
- AGPL-3.0 license (replaces MIT, per project decision).

### Changed
- License switched from MIT to GNU AGPL-3.0 — ensures modified versions
  running on network servers make their source code available.

## [1.0.0] — 2026-07-04

### Added
- Self-hosted portfolio dashboard: live prices, analyst targets, ETF-implied
  targets, technical signals (RSI 14, SMA 50/200 golden cross, ATH distance),
  fundamentals (P/E, P/B), performance bars (1D–48M), sparklines (3M/12M/5Y).
- Watchlist with same signal coverage as portfolio positions.
- Add/remove positions via UI, persisted to `config.json`.
- Rebalancing tab with simple allocation suggestion.
- Local SQLite price history store with stale-while-revalidate caching,
  batched downloads, capped concurrency with exponential backoff.
- Multi-currency support: EUR base, USD/GBP/GBp/JPY/CHF conversion (London
  pence handled correctly).
- `GET /api/summary` endpoint: compact agent/LLM-friendly report (JSON or
  markdown, ~4–10 KB, no history arrays) for multi-agent deliberation.
- Docker support (`Dockerfile`, `PORTFOLIO_DATA_DIR=/data`).
- Demo config (`config.example.json`) with 6 portfolio positions and 4
  watchlist items — auto-copied to `config.json` on first run.

### Fixed
- SQLite FD leak (`with sqlite3.connect` doesn't close — use `closing()`).
- iOS Safari tab crash from oversized hover-zoom canvases (guarded with
  `IS_MOBILE` / `pointer:coarse`).
- Body-level horizontal scroll on mobile (`max-width:100vw; overflow-x:hidden`).
- GBp/CHF conversion errors, fake 2y "ATH", impossible 36/48M performance bars.
