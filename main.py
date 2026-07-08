"""
Flatex Depot Tracker — Backend
Positions live in config.json; market data logic in marketdata.py.
API schema is unchanged from the original tracker, so index.html works as-is.
"""

import os
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


class BuyPriceIn(BaseModel):
    buy_price: float | None = None  # None or 0 clears the cost basis


@app.patch("/api/positions/portfolio/{ticker}")
def set_buy_price(ticker: str, body: BuyPriceIn):
    if not md.set_buy_price(ticker, body.buy_price):
        raise HTTPException(404, f"'{ticker}' nicht im Depot")
    return {"ok": True, "ticker": ticker, "buy_price": body.buy_price}


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
