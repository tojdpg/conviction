"""
Flatex Depot Tracker — Backend
Positions live in config.json; market data logic in marketdata.py.
API schema is unchanged from the original tracker, so index.html works as-is.
"""

import os
import re
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import yfinance as yf

import marketdata as md
from marketdata import PORTFOLIO, WATCHLIST, convert_to_base, sanitize

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app):
    def _prefetch_all():
        md.refresh_history(md.all_tickers(), force=True)
        md.get_fx_rates()
        items = [(p["ticker"], p["currency"]) for p in PORTFOLIO] + \
                [(w["ticker"], w["currency"]) for w in WATCHLIST]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(md.get_stock_data, t, c) for t, c in items]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception:
                    pass
    threading.Thread(target=_prefetch_all, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
md.init_db()


def _base_money_fields(kind, amount):
    """Return generic base-currency fields plus legacy *_eur aliases for index.html."""
    base = md.get_base_currency().lower()
    return {
        f"{kind}_{base}": amount,
        f"{kind}_base": amount,
        f"{kind}_eur": amount,
    }


def _base_response_fields(total=None):
    base = md.get_base_currency().lower()
    fields = {"base_currency": md.get_base_currency(), "currency_symbol": md.get_currency_symbol()}
    if total is not None:
        fields[f"total_value_{base}"] = total
        fields["total_value_base"] = total
        fields["total_value_eur"] = total
    return fields


@app.get("/api/config")
def get_config():
    return JSONResponse(md.get_public_config())


@app.get("/api/portfolio-lite")
def get_portfolio_lite():
    """Fast endpoint: prices + FX only. Serves from cache, fetches only misses."""
    rates = md.get_fx_rates()

    def fetch_price(pos):
        cached = md.cache_peek(pos["ticker"])
        if cached and cached.get("current_price"):
            keys = ("current_price", "ticker_currency", "short_name",
                    "recommendation", "kgv", "kvb")
            return pos, {k: cached.get(k) for k in keys}
        info = md.fetch_info(pos["ticker"], retries=1)
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        return pos, {
            "current_price": float(price) if price else None,
            "ticker_currency": info.get("currency", pos["currency"]),
            "short_name": info.get("shortName", pos["ticker"]),
            "recommendation": info.get("recommendationKey"),
            "kgv": info.get("trailingPE") or info.get("forwardPE"),
            "kvb": info.get("priceToBook"),
        }

    fetched = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for f in as_completed([ex.submit(fetch_price, p) for p in PORTFOLIO]):
            pos, data = f.result()
            fetched[pos["ticker"]] = data

    results, total_value = [], 0.0
    for pos in PORTFOLIO:
        data = fetched.get(pos["ticker"], {})
        price = data.get("current_price")
        ccy = data.get("ticker_currency", pos["currency"])
        price_base = convert_to_base(price, ccy, rates) if price else 0
        value_base = pos["shares"] * price_base
        total_value += value_base
        results.append({**pos, **data,
                        **_base_money_fields("price", round(price_base, 2)),
                        **_base_money_fields("value", round(value_base, 2)),
                        "pct_of_portfolio": 0})
    for r in results:
        if total_value > 0:
            r["pct_of_portfolio"] = round(r["value_base"] / total_value * 100, 2)
    results.sort(key=lambda x: -x["value_base"])

    return JSONResponse(sanitize({
        "portfolio": results,
        **_base_response_fields(round(total_value, 2)),
        "eurusd": round(rates["EURUSD=X"], 4),
        "timestamp": datetime.now().isoformat(),
        "lite": True,
    }))


def _find_close_for_days(history, days):
    """Close ~N days ago; oldest close if history covers >=90% of the span."""
    target = str((datetime.now() - timedelta(days=days)).date())
    close = None
    for h in history:
        if h["date"] <= target:
            close = h["close"]
        else:
            break
    if close is None and history:
        oldest_days = (datetime.now().date()
                       - datetime.strptime(history[0]["date"], "%Y-%m-%d").date()).days
        if oldest_days >= days * 0.9:
            close = history[0]["close"]
    return close


@app.get("/api/portfolio")
def get_portfolio():
    """Full portfolio: analyst data, signals, watchlist, period performance."""
    rates = md.get_fx_rates()
    md.refresh_history(md.all_tickers())  # no-op if refreshed <15 min ago
    theses = md.latest_theses()

    fetched = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(md.get_stock_data, p["ticker"], p["currency"], "2y"): p
                for p in PORTFOLIO}
        for f in as_completed(futs):
            fetched[futs[f]["ticker"]] = f.result()

    results, total_value = [], 0.0
    for pos in PORTFOLIO:
        data = fetched[pos["ticker"]]
        price = data.get("current_price")
        ccy = data.get("ticker_currency", pos["currency"])
        price_base = convert_to_base(price, ccy, rates) if price else 0
        value_base = pos["shares"] * price_base
        total_value += value_base
        results.append({**pos, **data,
                        **_base_money_fields("price", round(price_base, 2)),
                        **_base_money_fields("value", round(value_base, 2)),
                        "pct_of_portfolio": 0,
                        "thesis": theses.get(pos["ticker"])})
    for r in results:
        if total_value > 0:
            r["pct_of_portfolio"] = round(r["value_base"] / total_value * 100, 2)
    results.sort(key=lambda x: -x["value_base"])

    # Period performance from FULL stored history (36M/48M need >2y of data)
    periods = {"1d": 1, "7d": 7, "1m": 30, "3m": 91, "6m": 182,
               "12m": 365, "24m": 730, "36m": 1095, "48m": 1460}
    totals = {k: 0.0 for k in periods}
    for pos in PORTFOLIO:
        data = fetched[pos["ticker"]]
        full_hist = md.history_rows(pos["ticker"])
        if not full_hist:
            continue
        ccy = data.get("ticker_currency", pos["currency"])
        for key, days in periods.items():
            if key == "1d":
                close = full_hist[-2]["close"] if len(full_hist) >= 2 else full_hist[-1]["close"]
            else:
                close = _find_close_for_days(full_hist, days)
            if close:
                totals[key] += pos["shares"] * convert_to_base(close, ccy, rates)

    changes = {}
    for key in periods:
        t = totals[key]
        change = round(total_value - t, 2) if t else None
        changes[f"change_{key}_{md.get_base_currency().lower()}"] = change
        changes[f"change_{key}_base"] = change
        changes[f"change_{key}_eur"] = change
        changes[f"change_{key}_pct"] = round((total_value / t - 1) * 100, 2) if t else None

    fetched_wl = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(md.get_stock_data, w["ticker"], w["currency"], "1y"): w
                for w in WATCHLIST}
        for f in as_completed(futs):
            fetched_wl[futs[f]["ticker"]] = f.result()

    watchlist_results = []
    for w in WATCHLIST:
        data = fetched_wl[w["ticker"]]
        price = data.get("current_price")
        ccy = data.get("ticker_currency", w["currency"])
        price_base = convert_to_base(price, ccy, rates) if price else None
        watchlist_results.append({**w, **data,
                                  **_base_money_fields("price", round(price_base, 2) if price_base else None),
                                  "thesis": theses.get(w["ticker"])})

    return JSONResponse(sanitize({
        "portfolio": results,
        "watchlist": watchlist_results,
        **_base_response_fields(round(total_value, 2)),
        **changes,
        "eurusd": round(rates["EURUSD=X"], 4),
        "timestamp": datetime.now().isoformat(),
    }))


def _compact_row(pos, data, rates, with_value=True, theses=None):
    """Small, LLM-friendly view of one instrument (no history/sparklines)."""
    price = data.get("current_price")
    ccy = data.get("ticker_currency", pos["currency"])
    price_base = convert_to_base(price, ccy, rates) if price else None
    upside = None
    if data.get("analyst_target_mean") and price_base:
        upside = round((data["analyst_target_mean"] / price_base - 1) * 100, 1)
    elif data.get("etf_implied"):
        upside = data["etf_implied"].get("implied_upside")
    row = {
        "ticker": pos["ticker"],
        "name": data.get("short_name") or pos["name"],
        **_base_money_fields("price", round(price_base, 2) if price_base else None),
        "vs_ath_pct": data.get("ath_distance_pct"),
        "ath_date": data.get("ath_date"),
        "kgv": round(data["kgv"], 1) if data.get("kgv") else None,
        "kbv": round(data["kvb"], 2) if data.get("kvb") else None,
        "rsi": data.get("rsi"),
        "vs_sma50_pct": data.get("price_vs_sma50_pct"),
        "sma_signal": data.get("sma_signal"),
        "target_upside_pct": upside,
        "target_range_base": [data.get("analyst_target_low"), data.get("analyst_target_high")],
        "target_range_eur": [data.get("analyst_target_low"), data.get("analyst_target_high")],
        "recommendation": data.get("recommendation"),
        "analyst_count": data.get("analyst_count"),
        "thesis": (theses or {}).get(pos["ticker"]),
    }
    if with_value:
        row["shares"] = pos["shares"]
        buy = pos.get("buy_price")
        if buy and price_base:
            row["buy_price"] = buy
            row["gain_pct"] = round((price_base / buy - 1) * 100, 1)
    return row


def _summary_markdown(s):
    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "—"
    symbol = s.get("currency_symbol", md.get_currency_symbol())
    lines = [f"# Depot Summary ({s['timestamp'][:16]})",
             f"Gesamtwert: **{s['total_value_base']:,.0f} {symbol}** | EUR/USD {s['eurusd']}", ""]
    perf = " | ".join(f"{k.replace('change_', '').replace('_pct', '')}: {v:+.1f}%"
                      for k, v in s.items()
                      if k.startswith("change_") and k.endswith("_pct") and v is not None)
    lines += [f"Performance: {perf}", "", "## Positionen",
              f"| Ticker | Name | Stück | Kurs {symbol} | Wert {symbol} | % | vs.ATH | KGV | KBV | RSI | vs.SMA50 | Signal | Ziel-Upside |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in s["portfolio"]:
        lines.append("| " + " | ".join([
            r["ticker"], r["name"][:22], fmt(r.get("shares")), fmt(r["price_eur"]),
            fmt(r.get("value_eur")), fmt(r.get("pct_of_portfolio"), "%"),
            fmt(r["vs_ath_pct"], "%"), fmt(r["kgv"]), fmt(r["kbv"]), fmt(r["rsi"]),
            fmt(r["vs_sma50_pct"], "%"), fmt(r["sma_signal"]),
            fmt(r["target_upside_pct"], "%")]) + " |")
    lines += ["", "## Watchlist",
              f"| Ticker | Name | Kurs {symbol} | vs.ATH | KGV | KBV | RSI | vs.SMA50 | Signal | Ziel-Upside |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in s["watchlist"]:
        lines.append("| " + " | ".join([
            r["ticker"], r["name"][:22], fmt(r["price_eur"]), fmt(r["vs_ath_pct"], "%"),
            fmt(r["kgv"]), fmt(r["kbv"]), fmt(r["rsi"]), fmt(r["vs_sma50_pct"], "%"),
            fmt(r["sma_signal"]), fmt(r["target_upside_pct"], "%")]) + " |")
    return "\n".join(lines) + "\n"


@app.get("/api/summary")
def get_summary(format: str = "json"):
    """Agent-friendly compact summary: all signals, no history/sparkline arrays.
    format=md returns a markdown report (good for LLM context windows)."""
    rates = md.get_fx_rates()
    md.refresh_history(md.all_tickers())
    theses = md.latest_theses()

    with ThreadPoolExecutor(max_workers=8) as ex:
        pf = {p["ticker"]: f for p, f in
              [(p, ex.submit(md.get_stock_data, p["ticker"], p["currency"], "2y")) for p in PORTFOLIO]}
        wl = {w["ticker"]: f for w, f in
              [(w, ex.submit(md.get_stock_data, w["ticker"], w["currency"], "1y")) for w in WATCHLIST]}
        pf = {t: f.result() for t, f in pf.items()}
        wl = {t: f.result() for t, f in wl.items()}

    rows, total = [], 0.0
    for pos in PORTFOLIO:
        row = _compact_row(pos, pf[pos["ticker"]], rates, theses=theses)
        value = (row["price_base"] or 0) * pos["shares"]
        row.update(_base_money_fields("value", round(value, 0)))
        total += value
        rows.append(row)
    for r in rows:
        r["pct_of_portfolio"] = round(r["value_base"] / total * 100, 1) if total else None
    rows.sort(key=lambda x: -(x["value_base"] or 0))

    periods = {"1d": 1, "7d": 7, "1m": 30, "3m": 91, "6m": 182,
               "12m": 365, "24m": 730, "36m": 1095, "48m": 1460}
    changes = {}
    for key, days in periods.items():
        t = 0.0
        for pos in PORTFOLIO:
            hist = md.history_rows(pos["ticker"])
            if not hist:
                continue
            ccy = pf[pos["ticker"]].get("ticker_currency", pos["currency"])
            close = (hist[-2]["close"] if key == "1d" and len(hist) >= 2
                     else _find_close_for_days(hist, days))
            if close:
                t += pos["shares"] * convert_to_base(close, ccy, rates)
        changes[f"change_{key}_pct"] = round((total / t - 1) * 100, 2) if t else None

    summary = sanitize({
        "timestamp": datetime.now().isoformat(),
        **_base_response_fields(round(total, 0)),
        "eurusd": round(rates["EURUSD=X"], 4),
        **changes,
        "portfolio": rows,
        "watchlist": [_compact_row(w, wl[w["ticker"]], rates, with_value=False, theses=theses)
                      for w in WATCHLIST],
    })
    if format == "md":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(_summary_markdown(summary), media_type="text/markdown")
    return JSONResponse(summary)


class NewPosition(BaseModel):
    ticker: str
    shares: float | None = None  # None/0 -> watchlist, >0 -> portfolio
    name: str | None = None
    buy_price: float | None = None  # avg cost per share in base currency (portfolio only)


class ThesisIn(BaseModel):
    ticker: str
    verdict: Literal["buy-watch", "wait", "too-hot", "conditional", "sell-watch"]
    rationale: str
    author: str
    date: str | None = None


@app.post("/api/thesis")
def post_thesis(body: ThesisIn):
    ticker = body.ticker.strip().upper()
    rationale = body.rationale.strip()
    author = body.author.strip()
    if not ticker:
        raise HTTPException(400, "Ticker fehlt")
    if not rationale:
        raise HTTPException(400, "Rationale fehlt")
    if not author:
        raise HTTPException(400, "Author fehlt")
    entry = md.upsert_thesis(ticker, body.verdict, rationale, author, body.date)
    return {"ok": True, "thesis": entry}


@app.post("/api/positions")
def add_position(body: NewPosition):
    """Validate the ticker against Yahoo, then persist to config.json."""
    ticker = body.ticker.strip().upper()
    if not ticker:
        raise HTTPException(400, "Ticker fehlt")
    info = md.fetch_info(ticker, retries=1)
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    if not price:
        raise HTTPException(404, f"Ticker '{ticker}' bei Yahoo Finance nicht gefunden")
    currency = info.get("currency") or "USD"
    name = (body.name or "").strip() or info.get("shortName") or ticker

    if body.shares and body.shares > 0:
        list_name = "portfolio"
        entry = {"name": name, "ticker": ticker, "shares": body.shares, "currency": currency}
        if body.buy_price and body.buy_price > 0:
            entry["buy_price"] = body.buy_price
    else:
        list_name = "watchlist"
        entry = {"name": name, "ticker": ticker, "currency": currency}

    if not md.add_position(list_name, entry):
        raise HTTPException(409, f"'{ticker}' ist bereits in der Liste")

    def _warm():
        try:
            md.refresh_history([ticker], force=True)
            md.get_stock_data(ticker, currency)
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()

    return {"ok": True, "list": list_name, "ticker": ticker,
            "name": name, "currency": currency}


class PatchPositionIn(BaseModel):
    shares: float | None = None  # None = don't change
    buy_price: float | None = None  # None = don't change; 0 = clear


@app.patch("/api/positions/portfolio/{ticker}")
def patch_position(ticker: str, body: PatchPositionIn):
    if body.shares is None and body.buy_price is None:
        raise HTTPException(400, "Mindestens 'shares' oder 'buy_price' angeben")
    if not md.set_position(ticker, shares=body.shares, buy_price=body.buy_price):
        raise HTTPException(404, f"'{ticker}' nicht im Depot")
    return {"ok": True, "ticker": ticker, "shares": body.shares, "buy_price": body.buy_price}


class BulkItem(BaseModel):
    ticker: str
    shares: float | None = None
    buy_price: float | None = None
    name: str | None = None


class BulkImportIn(BaseModel):
    lines: str | None = None
    items: list[BulkItem] | None = None
    dry_run: bool = False


def _normalize_decimal(val: str) -> str:
    """Normalize German decimal comma (1.234,56 -> 1234.56)."""
    return val.replace(".", "").replace(",", ".")


def _parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip().replace("€", "").replace("$", "").replace("£", "").replace(" ", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        try:
            return float(_normalize_decimal(raw))
        except ValueError:
            return None


def _parse_line(line: str) -> BulkItem | None:
    """Parse a single line into a BulkItem. Supports multiple formats."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Format: "NVDA 10 @145.20" or "NVDA 10 145.20"
    m = re.match(r'^(\S+)\s+([\d.,]+)(?:\s*@\s*([\d.,]+))?\s*$', line)
    if m:
        ticker = m.group(1).strip().upper()
        shares = _parse_number(m.group(2))
        buy_price = _parse_number(m.group(3))
        if shares is not None and shares > 0:
            return BulkItem(ticker=ticker, shares=shares, buy_price=buy_price)

    # Format: "SAP.DE; 15; 120,50" (semicolon separated)
    parts = [p.strip() for p in line.split(";")]
    if len(parts) >= 2:
        ticker = parts[0].upper()
        shares = _parse_number(parts[1])
        buy_price = _parse_number(parts[2]) if len(parts) >= 3 else None
        if shares is not None and shares > 0:
            return BulkItem(ticker=ticker, shares=shares, buy_price=buy_price)

    # Format: "NVDA 10" (ticker + shares only)
    m = re.match(r'^(\S+)\s+([\d.,]+)\s*$', line)
    if m:
        ticker = m.group(1).strip().upper()
        shares = _parse_number(m.group(2))
        if shares is not None and shares > 0:
            return BulkItem(ticker=ticker, shares=shares)

    return None


def _detect_csv_delimiter(header: str) -> str:
    """Semicolons are more common in German CSV, but detect by trying both."""
    sc = header.count(";")
    cc = header.count(",")
    return ";" if sc >= cc else ","


CSV_TICKER_KEYS = {"ticker", "symbol", "isin"}
CSV_SHARES_KEYS = {"shares", "stück", "stueck", "anzahl", "menge"}
CSV_PRICE_KEYS = {"price", "preis", "einstand", "kaufkurs", "buy", "cost"}
CSV_NAME_KEYS = {"name", "bezeichnung", "description"}


def _parse_csv(lines: list[str]) -> list[BulkItem]:
    """Parse CSV block with header detection and flexible column mapping."""
    if len(lines) < 2:
        return []
    delim = _detect_csv_delimiter(lines[0])
    header = [h.strip().lower().strip('"').strip("'") for h in lines[0].split(delim)]
    col_ticker, col_shares, col_price, col_name = None, None, None, None
    for i, h in enumerate(header):
        if h in CSV_TICKER_KEYS and col_ticker is None:
            col_ticker = i
        elif h in CSV_SHARES_KEYS and col_shares is None:
            col_shares = i
        elif h in CSV_PRICE_KEYS and col_price is None:
            col_price = i
        elif h in CSV_NAME_KEYS and col_name is None:
            col_name = i
    if col_ticker is None:
        return []  # No recognizable header

    results = []
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(delim)]
        if len(parts) <= col_ticker:
            continue
        ticker = parts[col_ticker].upper().strip()
        if not ticker:
            continue
        shares = _parse_number(parts[col_shares]) if col_shares is not None and len(parts) > col_shares else None
        buy_price = _parse_number(parts[col_price]) if col_price is not None and len(parts) > col_price else None
        name = parts[col_name].strip() if col_name is not None and len(parts) > col_name and parts[col_name].strip() else None
        if shares is not None and shares > 0:
            results.append(BulkItem(ticker=ticker, shares=shares, buy_price=buy_price, name=name))
    return results


def _parse_lines(lines: str) -> list[BulkItem]:
    """Parse a multi-line string into BulkItem entries."""
    raw = [l for l in lines.split("\n") if l.strip() and not l.strip().startswith("#")]
    if not raw:
        return []

    # CSV detection: look for known header keywords in the first non-comment line
    first = raw[0].lower().strip()
    if any(kw in first for kw in ("ticker", "symbol", "stück", "shares", "einstand", "kaufkurs")):
        csv_items = _parse_csv(raw)
        if csv_items:
            return csv_items

    # Each line individually
    items = []
    for line in raw:
        item = _parse_line(line)
        if item:
            items.append(item)
    return items


def _validate_bulk_items(items: list[BulkItem], dry_run: bool) -> JSONResponse:
    """Validate each item via fetch_info and return preview or persist."""
    known_tickers = {p["ticker"] for p in md.PORTFOLIO}

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        def validate(item: BulkItem) -> dict:
            info = md.fetch_info(item.ticker, retries=1)
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
            if not price:
                return {"ticker": item.ticker, "status": "not_found"}
            if item.ticker in known_tickers:
                return {"ticker": item.ticker, "status": "duplicate"}
            return {
                "ticker": item.ticker,
                "name": info.get("shortName", item.ticker),
                "shares": item.shares,
                "buy_price": item.buy_price,
                "currency": info.get("currency", "USD"),
                "status": "ok",
            }

        futs = {ex.submit(validate, item): item for item in items}
        for f in as_completed(futs):
            results.append(f.result())

    # Stable order: match input sequence
    ticker_order = {item.ticker: i for i, item in enumerate(items)}
    results.sort(key=lambda r: ticker_order.get(r["ticker"], 999))

    if dry_run:
        return JSONResponse(results)

    ok_items = [r for r in results if r["status"] == "ok"]
    skipped_items = [r for r in results if r["status"] != "ok"]

    if not ok_items:
        return JSONResponse({"added": [], "skipped": skipped_items})

    entries = []
    for r in ok_items:
        entry = {
            "ticker": r["ticker"],
            "name": r["name"],
            "shares": r["shares"],
            "currency": r["currency"],
        }
        if r.get("buy_price"):
            entry["buy_price"] = r["buy_price"]
        entries.append(entry)

    added_tickers, skipped_dupes = md.add_positions_batch(entries)

    # Warm cache in background
    def _warm():
        for r in ok_items:
            try:
                md.refresh_history([r["ticker"]], force=True)
                md.get_stock_data(r["ticker"], r["currency"])
            except Exception:
                pass
    threading.Thread(target=_warm, daemon=True).start()

    return JSONResponse({
        "added": [r for r in ok_items if r["ticker"] in added_tickers],
        "skipped": skipped_items + [r for r in ok_items if r["ticker"] in skipped_dupes],
    })


@app.post("/api/positions/bulk")
def bulk_import(body: BulkImportIn):
    """Bulk-import positions from lines (multi-line string) or items (JSON array)."""
    if body.lines is not None and body.items is not None:
        raise HTTPException(400, "Nur 'lines' ODER 'items' angeben, nicht beide")
    if body.lines is None and body.items is None:
        raise HTTPException(400, "'lines' oder 'items' erforderlich")

    if body.lines is not None:
        items = _parse_lines(body.lines)
    else:
        items = body.items or []

    if not items:
        raise HTTPException(400, "Keine gültigen Positionen gefunden")

    return _validate_bulk_items(items, dry_run=body.dry_run)


@app.delete("/api/positions/{list_name}/{ticker}")
def delete_position(list_name: str, ticker: str):
    if list_name not in ("portfolio", "watchlist"):
        raise HTTPException(400, "Liste muss 'portfolio' oder 'watchlist' sein")
    if not md.remove_position(list_name, ticker):
        raise HTTPException(404, f"'{ticker}' nicht in {list_name} gefunden")
    return {"ok": True}


@app.get("/api/history/{ticker}")
def get_history(ticker: str, period: str = "2y"):
    """Historical closes, from the local store (Yahoo fallback for unknowns)."""
    period_days = {"1m": 30, "3m": 91, "6m": 182, "1y": 365, "2y": 730,
                   "5y": 1825, "max": None}
    days = period_days.get(period, 730)
    rows = md.history_rows(ticker, days=days)
    if rows:
        return JSONResponse({"ticker": ticker,
                             "data": [{"date": r["date"], "close": round(r["close"], 2)}
                                      for r in rows]})
    try:
        hist = yf.Ticker(ticker).history(period=period)
        data = [{"date": str(d.date()), "close": round(float(row["Close"]), 2)}
                for d, row in hist.iterrows()]
        return JSONResponse({"ticker": ticker, "data": data})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/analysts/{ticker}")
def get_analyst_detail(ticker: str):
    """Detailed analyst info for one instrument."""
    try:
        info = md.fetch_info(ticker)
        rates = md.get_fx_rates()
        ccy = info.get("currency", "USD")

        rec_data = []
        try:
            recs = yf.Ticker(ticker).recommendations
            if recs is not None and len(recs) > 0:
                for idx, row in recs.tail(20).iterrows():
                    rec_data.append({
                        "date": str(idx),
                        "firm": str(row.get("Firm", "")),
                        "to_grade": str(row.get("To Grade", "")),
                        "from_grade": str(row.get("From Grade", "")),
                        "action": str(row.get("Action", "")),
                    })
        except Exception:
            pass

        def to_base(val):
            return round(convert_to_base(float(val), ccy, rates), 2) if val is not None else None

        return JSONResponse(sanitize({
            "ticker": ticker,
            "short_name": info.get("shortName", ticker),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "dividend_yield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "base_currency": md.get_base_currency(),
            "currency_symbol": md.get_currency_symbol(),
            "52w_high": to_base(info.get("fiftyTwoWeekHigh")),
            "52w_low": to_base(info.get("fiftyTwoWeekLow")),
            "50d_avg": to_base(info.get("fiftyDayAverage")),
            "200d_avg": to_base(info.get("twoHundredDayAverage")),
            "analyst_target_low": to_base(info.get("targetLowPrice")),
            "analyst_target_mean": to_base(info.get("targetMeanPrice")),
            "analyst_target_high": to_base(info.get("targetHighPrice")),
            "analyst_target_median": to_base(info.get("targetMedianPrice")),
            "analyst_count": info.get("numberOfAnalystOpinions"),
            "recommendation_key": info.get("recommendationKey"),
            "recommendation_mean": info.get("recommendationMean"),
            "earnings_growth": info.get("earningsGrowth"),
            "revenue_growth": info.get("revenueGrowth"),
            "profit_margins": info.get("profitMargins"),
            "analyst_recommendations": rec_data,
        }))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/")
def serve_frontend():
    with open(os.path.join(BASE_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,
                host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8080")))
