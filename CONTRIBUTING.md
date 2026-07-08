# Contributing to conviction

Thanks for your interest in contributing. This document explains what we accept,
what we don't, and how to submit changes.

## Scope guardrails — what this project is and is not

**conviction is a buy/sell decision cockpit, not a portfolio accounting tool.**
The minimalism is the product. These guardrails prevent feature creep that would
turn a signal dashboard into a brokerage platform.

### Accepted contributions

- Bug fixes, performance improvements, UI/UX refinements
- New data sources or signal types (RSI variants, MACD, Bollinger bands, etc.)
- Improvements to the agent API (`/api/summary` and future endpoints)
- Documentation, translations, accessibility
- Docker/CI/CD improvements
- Config flexibility (base currency, buy-rule thresholds, etc.)

### Rejected contributions (will not be merged)

- **Brokerage account integration** — no links to Interactive Brokers, Flatex,
  Trade Republic, or any broker API. Positions are entered manually or via
  `config.json`. This is by design: no credentials, no tokens, no attack surface.
- **Transaction import** — no CSV/OFX/QIF import of buy/sell transactions.
  The tracker shows current state (positions + signals), not historical accounting.
  Cost basis (`buy_price` per position) is the only planned exception, and only as
  a single optional field — not a transaction ledger.
- **Transaction history / accounting** — no realized P&L, no tax-lot tracking,
  no dividend history. This is a decision tool, not a tax tool.
- **Authentication / multi-user** — the app is single-user, localhost-first.
  If you need remote access, use a VPN (Tailscale/WireGuard). Don't add auth.
- **Backend database** — SQLite is the store, deliberately. No Postgres, no Redis,
  no external database server.

If you're unsure whether your idea fits, open an issue and ask before coding.

---

## Development setup

```bash
git clone https://github.com/tojdpg/portfoliotracker.git
cd portfoliotracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

The server starts on `http://localhost:8080` with the demo config. Edit
`config.json` (auto-created from `config.example.json` on first run) to test with
your own tickers.

### Code style

- Python: formatted with [ruff](https://github.com/astral-sh/ruff). Run
  `ruff check .` before submitting.
- Frontend: `index.html` is a single file (Tailwind CDN + Chart.js + vanilla JS).
  Keep it that way — no build step, no npm.
- API schema is frozen: response field names (including legacy spellings like
  `kvb` for P/B) must not change without coordinating frontend + backend together.

---

## Developer Certificate of Origin (DCO)

All contributions must be signed off with the Developer Certificate of Origin,
version 1.1:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
1 Letterman Drive
Suite D4700
San Francisco, CA, 94129

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the license of this project; or

(b) The contribution is based upon a previous work that, to the best
    of my knowledge, I have the right to submit under the license of
    this project; or

(c) The contribution was submitted directly to me by some other person
    who certified (a), (b) or (c) and I have not modified it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and can be redistributed consistent with
    this project or the license(s) involved.

(e) I hereby grant to the project's copyright holders and licensees
    the rights to distribute and use the contribution as described
    above, and to relicense it under the same terms as the project
    (AGPL-3.0 or any later version).
```

### How to sign off

Add `Signed-off-by: Your Name <your.email@example.com>` to the end of your commit
message. Git can do this automatically:

```bash
git commit -s -m "Fix: correct GBp conversion for LSE tickers"
```

PRs without at least one `Signed-off-by` line on every commit will be rejected.

---

## Privacy

- **Never commit `config.json`** — it contains your portfolio positions. The file
  is gitignored; verify with `git status` before pushing.
- **Never commit `prices.db` or `.cache.pkl`** — these may contain cached data
  tied to your tickers.
- **No real names or personal data** in code, commits, issues, or screenshots.
  If you need to show a screenshot for a bug report, use the demo config.

---

## Reporting bugs

Use the [issue templates](.github/ISSUE_TEMPLATE/) provided. Include:
- Steps to reproduce
- Expected vs. actual behavior
- Ticker symbols involved (use demo tickers if possible)
- Browser + OS if it's a frontend issue
- Relevant log output (no personal data)
