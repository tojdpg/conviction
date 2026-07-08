"""
Market data layer for the depot tracker.

- SQLite price store (prices.db): full history per ticker, survives Yahoo outages
- Hardened yfinance access: batch downloads, semaphore-limited .info calls with backoff
- Correct FX handling incl. minor units (GBp pence -> GBP)
- Analyst targets, ETF-implied targets (live holdings with static fallback),
  manual BTC/gold targets from config.json (upside computed against live spot)
"""

import json
import math
import os
import pickle
import random
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# All mutable state (config, price DB, cache) lives here; override for Docker etc.
DATA_DIR = os.environ.get("PORTFOLIO_DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "prices.db")
CACHE_FILE = os.path.join(DATA_DIR, ".cache.pkl")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
THESES_FILE = os.path.join(DATA_DIR, "theses.json")

if not os.path.exists(CONFIG_FILE):
    shutil.copyfile(os.path.join(BASE_DIR, "config.example.json"), CONFIG_FILE)
    print(f"config.json angelegt ({CONFIG_FILE}) — Demo-Positionen, bitte anpassen "
          "(Datei editieren oder ＋ Position im Frontend).")

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

PORTFOLIO = CONFIG["portfolio"]
WATCHLIST = CONFIG["watchlist"]

DEFAULT_BUY_RULES = {
    "rsi": {"buy_below": 30, "avoid_above": 70},
    "kgv": {"cheap_below": 15, "expensive_above": 25},
    "kbv": {"cheap_below": 1.5, "expensive_above": 3},
    "vs_sma50": {"ideal_band": 2, "stretched_above": 8},
    "ath": {"strong_buy_below": -50, "buy_band": [-30, -20]},
}

CURRENCY_SYMBOLS = {
    "EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CHF": "CHF",
    "CAD": "C$", "AUD": "A$", "SEK": "kr", "NOK": "kr", "DKK": "kr",
}

CACHE_TTL = 900        # fresh
STALE_TTL = 3600       # stale but usable, refresh in background
ETF_TARGET_TTL = 86400 # implied targets need ~10 .info calls each; refresh daily

_config_lock = threading.Lock()
_thesis_lock = threading.Lock()


def get_base_currency():
    return str(CONFIG.get("base_currency", "EUR") or "EUR").upper()


def get_language():
    return str(CONFIG.get("language", "de") or "de")


def get_currency_symbol(currency=None):
    currency = (currency or get_base_currency()).upper()
    return CURRENCY_SYMBOLS.get(currency, currency)


def get_buy_rules():
    """Configurable threshold block, merged over safe defaults."""
    rules = json.loads(json.dumps(DEFAULT_BUY_RULES))
    for section, values in CONFIG.get("buy_rules", {}).items():
        if isinstance(values, dict) and isinstance(rules.get(section), dict):
            rules[section].update(values)
        else:
            rules[section] = values
    return rules


def get_public_config():
    base = get_base_currency()
    return {
        "base_currency": base,
        "symbol": get_currency_symbol(base),
        "language": get_language(),
        "buy_rules": get_buy_rules(),
    }


def _save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(CONFIG, f, indent=2, ensure_ascii=False)
        f.write("\n")


def add_position(list_name, entry):
    """Append to portfolio/watchlist (in place, so imported refs stay valid)."""
    with _config_lock:
        lst = CONFIG[list_name]
        if any(x["ticker"] == entry["ticker"] for x in lst):
            return False
        lst.append(entry)
        _save_config()
    return True


def set_buy_price(ticker, buy_price):
    """Set or clear the avg cost per share (base currency) on a portfolio position."""
    with _config_lock:
        for pos in CONFIG["portfolio"]:
            if pos["ticker"] == ticker:
                if buy_price and buy_price > 0:
                    pos["buy_price"] = buy_price
                else:
                    pos.pop("buy_price", None)
                _save_config()
                return True
    return False


def remove_position(list_name, ticker):
    with _config_lock:
        lst = CONFIG[list_name]
        kept = [x for x in lst if x["ticker"] != ticker]
        if len(kept) == len(lst):
            return False
        lst[:] = kept
        _save_config()
    return True


def _load_theses():
    try:
        with open(THESES_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], dict):
        return data["items"]
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {f"{x.get('ticker', '').upper()}::{x.get('author', '')}": x
                for x in data if isinstance(x, dict) and x.get("ticker") and x.get("author")}
    return {}


def _save_theses(items):
    with open(THESES_FILE, "w") as f:
        json.dump({"items": items}, f, indent=2, ensure_ascii=False)
        f.write("\n")


def upsert_thesis(ticker, verdict, rationale, author, date=None):
    ticker = ticker.strip().upper()
    author = author.strip()
    entry = {
        "ticker": ticker,
        "verdict": verdict,
        "rationale": rationale.strip(),
        "author": author,
        "date": date or datetime.now().date().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    with _thesis_lock:
        items = _load_theses()
        items[f"{ticker}::{author}"] = entry
        _save_theses(items)
    return entry


def latest_theses():
    """Newest thesis per ticker across authors."""
    items = _load_theses().values()
    latest = {}
    for entry in items:
        if not isinstance(entry, dict) or not entry.get("ticker"):
            continue
        ticker = entry["ticker"].upper()
        prev = latest.get(ticker)
        stamp = entry.get("updated_at", entry.get("date", ""))
        prev_stamp = prev.get("updated_at", prev.get("date", "")) if prev else ""
        if prev is None or stamp > prev_stamp:
            latest[ticker] = entry
    return latest


def latest_thesis(ticker):
    return latest_theses().get(ticker.upper())

# History tickers needed beyond portfolio/watchlist (FX for ATH-date conversion, gold spot)
AUX_TICKERS = ["EURUSD=X", "GC=F"]


def sanitize(obj):
    """Replace NaN/Inf with None for JSON serialization."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


# ── Result cache (stale-while-revalidate, pickle-persisted) ──────────────────

_cache_lock = threading.Lock()
_refreshing = set()


def _load_cache():
    try:
        with open(CACHE_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return {}


_cache = _load_cache()


def _save_cache():
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(_cache, f)
    except Exception:
        pass


def cache_get(key, ttl=CACHE_TTL, stale_ttl=STALE_TTL):
    with _cache_lock:
        if key in _cache:
            val, ts = _cache[key]
            age = time.time() - ts
            if age < ttl:
                return val, False
            if age < stale_ttl:
                return val, True
            del _cache[key]
    return None, False


def cache_set(key, val):
    with _cache_lock:
        _cache[key] = (val, time.time())
        _refreshing.discard(key)
        _save_cache()


# ── FX ────────────────────────────────────────────────────────────────────────

FX_FALLBACK_PER_EUR = {"EUR": 1.0, "USD": 1.16, "GBP": 0.85, "JPY": 180.0, "CHF": 0.93}
COMMON_FX_CURRENCIES = {"EUR", "USD", "GBP", "JPY", "CHF"}

# Yahoo quotes some exchanges in minor units (LSE in pence)
_MINOR_UNITS = {"GBp": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

_fx_lock = threading.Lock()
_fx_cache = {}
_fx_ts = 0.0
_FX_TTL = 300


def normalize_ccy(price, currency):
    """Convert minor-unit quotes (GBp pence) to major unit (GBP)."""
    if price is not None and currency in _MINOR_UNITS:
        major, div = _MINOR_UNITS[currency]
        return price / div, major
    return price, currency


def _major_currency(currency):
    if currency in _MINOR_UNITS:
        return _MINOR_UNITS[currency][0]
    return str(currency or get_base_currency()).upper()


def _configured_currencies(extra=None):
    currencies = {get_base_currency(), *COMMON_FX_CURRENCIES}
    for item in PORTFOLIO + WATCHLIST:
        currencies.add(_major_currency(item.get("currency")))
    for currency in extra or []:
        currencies.add(_major_currency(currency))
    return {c for c in currencies if c}


def _pair_rate_from_eur_fallback(from_ccy, to_ccy):
    from_per_eur = FX_FALLBACK_PER_EUR.get(from_ccy)
    to_per_eur = FX_FALLBACK_PER_EUR.get(to_ccy)
    if from_per_eur and to_per_eur:
        return to_per_eur / from_per_eur
    return None


def _fx_pairs_for(currencies=None):
    currencies = _configured_currencies(currencies)
    pairs = {"EURUSD=X"}
    base = get_base_currency()
    for currency in currencies:
        if currency == base:
            continue
        pairs.add(f"{currency}{base}=X")
        pairs.add(f"{base}{currency}=X")
    return pairs


def _fallback_rates(pairs):
    rates = {}
    for pair in pairs:
        raw = pair[:-2] if pair.endswith("=X") else pair
        if len(raw) != 6:
            continue
        rate = _pair_rate_from_eur_fallback(raw[:3], raw[3:])
        if rate:
            rates[pair] = rate
    return rates


def get_fx_rates(currencies=None):
    """Dynamic FX rates as Yahoo pairs (TO per 1 FROM), cached for 5 minutes."""
    global _fx_cache, _fx_ts
    pairs = _fx_pairs_for(currencies)
    with _fx_lock:
        if _fx_cache and pairs.issubset(_fx_cache.keys()) and time.time() - _fx_ts < _FX_TTL:
            return _fx_cache
        rates = _fallback_rates(pairs)
        try:
            df = yf.download(sorted(pairs), period="5d", progress=False,
                             auto_adjust=True, group_by="ticker", threads=True)
            for pair in pairs:
                try:
                    closes = (df[pair]["Close"] if isinstance(df.columns, pd.MultiIndex)
                              else df["Close"]).dropna()
                    if len(closes) > 0:
                        rates[pair] = float(closes.iloc[-1])
                except Exception:
                    pass
        except Exception:
            pass
        _fx_cache, _fx_ts = rates, time.time()
        return rates


def get_eurusd_rate():
    return get_fx_rates(["USD"])["EURUSD=X"]


def fx_rate_on(from_ccy, to_ccy, date_str):
    """FX close on or before a date (TO per 1 FROM), if stored."""
    from_ccy, to_ccy = _major_currency(from_ccy), _major_currency(to_ccy)
    if from_ccy == to_ccy:
        return 1.0
    pair = f"{from_ccy}{to_ccy}=X"
    inverse = f"{to_ccy}{from_ccy}=X"
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT close FROM prices WHERE ticker = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1", (pair, date_str)).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT close FROM prices WHERE ticker = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1", (inverse, date_str)).fetchone()
        if row and row[0]:
            return 1 / row[0]
    return None


def _rate_from_map(rates, from_ccy, to_ccy):
    if from_ccy == to_ccy:
        return 1.0
    direct = rates.get(f"{from_ccy}{to_ccy}=X")
    if direct:
        return direct
    inverse = rates.get(f"{to_ccy}{from_ccy}=X")
    if inverse:
        return 1 / inverse
    return None


def convert_to_base(price, currency, rates=None, base_currency=None, date_str=None):
    """Convert a price in any supported currency (incl. GBp) to the configured base."""
    if price is None:
        return price
    price, currency = normalize_ccy(price, currency)
    currency = _major_currency(currency)
    base_currency = _major_currency(base_currency or get_base_currency())
    if currency == base_currency:
        return price
    if rates is None:
        rates = get_fx_rates([currency, base_currency])
    rate = fx_rate_on(currency, base_currency, date_str) if date_str else None
    rate = rate or _rate_from_map(rates, currency, base_currency)
    if rate:
        return price * rate
    if currency != "USD" and base_currency != "USD":
        to_usd = _rate_from_map(rates, currency, "USD")
        usd_to_base = _rate_from_map(rates, "USD", base_currency)
        if to_usd and usd_to_base:
            return price * to_usd * usd_to_base
    fallback = _pair_rate_from_eur_fallback(currency, base_currency)
    return price * fallback if fallback else price


def convert_to_eur(price, currency, rates=None):
    """Backward-compatible explicit EUR conversion."""
    return convert_to_base(price, currency, rates, base_currency="EUR")


# ── SQLite price store ────────────────────────────────────────────────────────

_db_write_lock = threading.Lock()


def _conn():
    # NB: sqlite3's own context manager only wraps a transaction, it does NOT
    # close the connection — always use `with closing(_conn())` or FDs leak.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db_write_lock, closing(_conn()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL, date TEXT NOT NULL,
            close REAL NOT NULL, high REAL, low REAL,
            PRIMARY KEY (ticker, date))""")


def upsert_history(ticker, df):
    if df is None or len(df) == 0:
        return
    rows = []
    for date, row in df.iterrows():
        c = row.get("Close")
        if c is None or pd.isna(c):
            continue
        h, l = row.get("High"), row.get("Low")
        rows.append((ticker, str(pd.Timestamp(date).date()), float(c),
                     float(h) if pd.notna(h) else None,
                     float(l) if pd.notna(l) else None))
    if not rows:
        return
    with _db_write_lock, closing(_conn()) as conn, conn:
        conn.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?)", rows)


def history_rows(ticker, days=None):
    """Ordered [{date, close, high, low}] from the store."""
    q = "SELECT date, close, high, low FROM prices WHERE ticker = ?"
    args = [ticker]
    if days:
        q += " AND date >= ?"
        args.append(str((datetime.now() - timedelta(days=days)).date()))
    q += " ORDER BY date"
    with closing(_conn()) as conn:
        rows = conn.execute(q, args).fetchall()
    return [{"date": r[0], "close": r[1], "high": r[2], "low": r[3]} for r in rows]


def last_stored_date(ticker):
    with closing(_conn()) as conn:
        row = conn.execute("SELECT MAX(date) FROM prices WHERE ticker = ?", (ticker,)).fetchone()
    return row[0] if row and row[0] else None


def ath_row(ticker):
    """(high, date) of the true all-time high in the store."""
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT high, date FROM prices WHERE ticker = ? AND high IS NOT NULL "
            "ORDER BY high DESC, date ASC LIMIT 1", (ticker,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def eurusd_on(date_str):
    """EUR/USD close on or before a date (from stored history)."""
    return fx_rate_on("EUR", "USD", date_str)


def _download_and_store(tickers, **kwargs):
    if not tickers:
        return
    try:
        df = yf.download(tickers, progress=False, auto_adjust=True,
                         group_by="ticker", threads=True, **kwargs)
    except Exception:
        return
    if df is None or len(df) == 0:
        return
    if isinstance(df.columns, pd.MultiIndex):
        for t in {c[0] for c in df.columns}:
            try:
                upsert_history(t, df[t].dropna(how="all"))
            except Exception:
                pass
    else:
        upsert_history(tickers[0], df)


_hist_lock = threading.Lock()
_last_hist_refresh = 0.0


def refresh_history(tickers, force=False):
    """Backfill new tickers with full history, top up known ones incrementally."""
    global _last_hist_refresh
    with _hist_lock:
        if not force and time.time() - _last_hist_refresh < CACHE_TTL:
            return
        new, known = [], []
        for t in tickers:
            last = last_stored_date(t)
            (known if last else new).append((t, last))
        _download_and_store([t for t, _ in new], period="max")
        if known:
            oldest = min(last for _, last in known)
            start = datetime.strptime(oldest, "%Y-%m-%d") - timedelta(days=7)
            _download_and_store([t for t, _ in known], start=start.strftime("%Y-%m-%d"))
        _last_hist_refresh = time.time()


def all_tickers():
    return [p["ticker"] for p in PORTFOLIO] + [w["ticker"] for w in WATCHLIST] + AUX_TICKERS + sorted(_fx_pairs_for())


# ── Rate-limit-safe .info access ─────────────────────────────────────────────

_info_sem = threading.Semaphore(4)


def fetch_info(ticker, retries=3):
    """yf .info with concurrency cap and exponential backoff (Yahoo 429s)."""
    delay = 2.0
    for attempt in range(retries):
        try:
            with _info_sem:
                info = yf.Ticker(ticker).info or {}
            if info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose"):
                return info
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(delay + random.random())
            delay *= 3
    return {}


def spot_from_store(ticker):
    rows = history_rows(ticker, days=14)
    return rows[-1]["close"] if rows else None


# ── Implied / manual targets ─────────────────────────────────────────────────

def _live_holdings(ticker):
    """Top holdings (symbol, weight) live from Yahoo fund data, if available."""
    try:
        th = yf.Ticker(ticker).funds_data.top_holdings
        if th is not None and len(th) > 0:
            col = "Holding Percent" if "Holding Percent" in th.columns else th.columns[-1]
            out = [(str(idx), float(th.loc[idx, col])) for idx in th.index]
            return [(s, w) for s, w in out if w and w > 0][:10] or None
    except Exception:
        pass
    return None


def _weighted_target(holdings, name, live):
    """Weighted average analyst upside across (ticker, weight) holdings."""
    total_weight, weighted_upside, upsides = 0.0, 0.0, []
    for ticker, weight in holdings:
        info = fetch_info(ticker, retries=1)
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        target = info.get("targetMeanPrice")
        if not (price and target and float(price) > 0):
            continue
        upside = (float(target) / float(price) - 1) * 100
        weighted_upside += upside * weight
        total_weight += weight
        upsides.append({
            "ticker": ticker, "upside": round(upside, 1), "weight": weight,
            "target_low": float(info["targetLowPrice"]) if info.get("targetLowPrice") else None,
            "target_high": float(info["targetHighPrice"]) if info.get("targetHighPrice") else None,
            "target_mean": float(target), "price": float(price),
        })
    if total_weight <= 0 or len(upsides) < 3:
        return None
    min_up = min(u["upside"] for u in upsides)
    max_up = max(u["upside"] for u in upsides)
    return {
        "implied_upside": round(weighted_upside / total_weight, 1),
        "holdings_analyzed": len(upsides),
        "total_weight": round(total_weight * 100, 1),
        "source": f"Gewichtet aus {len(upsides)} Top-Holdings ({name}, {'live' if live else 'statisch'})",
        "range_low": 100 * (1 + min_up / 100),
        "range_high": 100 * (1 + max_up / 100),
        "min_upside": round(min_up, 1),
        "max_upside": round(max_up, 1),
        "holdings": upsides,
    }


def _bitcoin_target():
    cfg = CONFIG["manual_targets"]["bitcoin"]
    spot = spot_from_store("BTC-USD")
    if not spot:
        return None
    up = lambda t: round((t / spot - 1) * 100, 1)
    return {
        "implied_upside": up(cfg["target_12m"]),
        "holdings_analyzed": 5,
        "total_weight": 100,
        "source": f"{cfg['source']} (Stand {cfg['as_of']})",
        "target_6m": cfg["target_6m"],
        "target_12m": cfg["target_12m"],
        "range_low": cfg["target_low"],
        "range_high": cfg["target_high"],
        "min_upside": up(cfg["target_low"]),
        "max_upside": up(cfg["target_high"]),
    }


def get_etf_implied_target(ticker):
    """Implied target for ETFs/ETPs without analyst coverage. 24h cached."""
    key = f"etf_{ticker}"
    cached, _ = cache_get(key, ttl=ETF_TARGET_TTL, stale_ttl=ETF_TARGET_TTL)
    if cached is not None:
        return cached or None  # {} marks a cached negative
    if ticker in CONFIG["manual_targets"]["bitcoin"]["tickers"]:
        result = _bitcoin_target()
    elif ticker in CONFIG["etf_targets"]:
        cfg = CONFIG["etf_targets"][ticker]
        live = _live_holdings(ticker)
        result = _weighted_target(live or [tuple(h) for h in cfg["holdings"]],
                                  cfg["name"], live=bool(live))
    else:
        result = None
    cache_set(key, result if result else {})
    return result


def _apply_gold_target(result, rates):
    """Analyst-style targets for the gold ETC, scaled from gold spot forecasts."""
    cfg = CONFIG["manual_targets"]["gold"]
    spot = spot_from_store(cfg["spot_ticker"])
    cp = result.get("current_price")
    if not spot or not cp:
        return
    ccy = result.get("ticker_currency")
    low = cp * cfg["target_low_usd"] / spot
    mean = cp * cfg["target_mean_usd"] / spot
    high = cp * cfg["target_high_usd"] / spot
    result["analyst_target_low"] = round(convert_to_base(low, ccy, rates), 2)
    result["analyst_target_mean"] = round(convert_to_base(mean, ccy, rates), 2)
    result["analyst_target_high"] = round(convert_to_base(high, ccy, rates), 2)
    result["analyst_count"] = 5
    result["recommendation"] = "buy" if cfg["target_mean_usd"] / spot > 1.1 else "hold"
    result["etf_implied"] = {
        "implied_upside": round((cfg["target_mean_usd"] / spot - 1) * 100, 1),
        "holdings_analyzed": 5,
        "total_weight": 100,
        "source": f"{cfg['source']} (Stand {cfg['as_of']}, Spot live)",
    }


# ── Per-ticker assembly (same schema as the original tracker) ────────────────

def _assemble_stock(ticker_str, pos_currency="USD", period="2y"):
    result = {
        "ticker": ticker_str,
        "current_price": None,
        "ath": None, "ath_date": None, "ath_distance_pct": None,
        "analyst_target_low": None, "analyst_target_mean": None,
        "analyst_target_high": None, "analyst_target_median": None,
        "analyst_count": None, "recommendation": None, "recommendation_summary": None,
        "history": [], "error": None,
    }
    try:
        rates = get_fx_rates()
        info = fetch_info(ticker_str)
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        result["current_price"] = float(price) if price else None
        ticker_ccy = info.get("currency", pos_currency)
        result["ticker_currency"] = ticker_ccy

        # History from the store (charts use the requested period slice)
        period_days = {"1y": 365, "2y": 730, "5y": 1825, "max": None}
        hist = history_rows(ticker_str, days=period_days.get(period, 730))
        result["history"] = [
            {"date": h["date"], "close": round(h["close"], 2),
             "high": round(h["high"], 2) if h["high"] is not None else None,
             "low": round(h["low"], 2) if h["low"] is not None else None}
            for h in hist
        ]

        # True ATH from full stored history, converted to the configured base at the ATH-date FX
        ath_raw, ath_date = ath_row(ticker_str)
        if ath_raw:
            ath_base = round(convert_to_base(ath_raw, ticker_ccy, rates, date_str=ath_date), 2)
            result["ath"] = ath_base
            result["ath_raw"] = ath_raw
            result["ath_currency"] = ticker_ccy
            result["ath_date"] = ath_date
            if result["current_price"]:
                current_base = convert_to_base(result["current_price"], ticker_ccy, rates)
                result["ath_distance_pct"] = round((current_base / ath_base - 1) * 100, 2)

        # Sparkline: since ATH if it lies within the chart window, else last 90 days
        if hist:
            MIN_SPARK_POINTS = 5
            ath_idx = next((i for i, h in enumerate(hist) if h["date"] == result.get("ath_date")), None)
            ath_age_days = 999
            if result.get("ath_date"):
                ath_age_days = (datetime.now() - datetime.strptime(result["ath_date"], "%Y-%m-%d")).days
            pts_since_ath = len(hist) - ath_idx if ath_idx is not None else 0
            from_ath = ath_idx is not None and ath_age_days >= 1 and pts_since_ath >= MIN_SPARK_POINTS
            spark = hist[ath_idx:] if from_ath else hist[-90:]
            result["sparkline"] = [{"date": h["date"], "close": round(h["close"], 2)} for h in spark]
            result["sparkline_from_ath"] = from_ath

        # SMA 50/200, RSI 14 (simple average, as before)
        closes = [h["close"] for h in history_rows(ticker_str, days=430)]
        if len(closes) >= 50:
            result["sma_50"] = round(sum(closes[-50:]) / 50, 2)
        if len(closes) >= 200:
            result["sma_200"] = round(sum(closes[-200:]) / 200, 2)
        sma50, sma200, cp = result.get("sma_50"), result.get("sma_200"), result.get("current_price")
        if sma50 and sma200 and cp:
            result["sma_diff_pct"] = round((sma50 - sma200) / sma200 * 100, 1)
            if sma50 > sma200 and cp > sma200:
                result["sma_signal"] = "golden_cross"
            elif cp > sma200:
                result["sma_signal"] = "bullish"
            elif cp > sma50:
                result["sma_signal"] = "entry"
            else:
                result["sma_signal"] = "bearish"
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))][-14:]
            avg_gain = sum(d for d in deltas if d > 0) / 14
            avg_loss = sum(-d for d in deltas if d < 0) / 14 or 0.001
            result["rsi"] = round(100 - (100 / (1 + avg_gain / avg_loss)), 1)
        if result.get("sma_50") and cp:
            result["price_vs_sma50_pct"] = round((cp / result["sma_50"] - 1) * 100, 1)

        # Analyst targets in configured base currency (currency-correct incl. pence)
        def to_base(val):
            return round(convert_to_base(float(val), ticker_ccy, rates), 2) if val is not None else None

        result["analyst_target_low"] = to_base(info.get("targetLowPrice"))
        result["analyst_target_mean"] = to_base(info.get("targetMeanPrice"))
        result["analyst_target_high"] = to_base(info.get("targetHighPrice"))
        result["analyst_target_median"] = to_base(info.get("targetMedianPrice"))
        result["analyst_count"] = int(info["numberOfAnalystOpinions"]) if info.get("numberOfAnalystOpinions") else None
        result["recommendation"] = info.get("recommendationKey")
        result["kgv"] = info.get("trailingPE") or info.get("forwardPE")
        result["kvb"] = info.get("priceToBook")  # key name kept: frontend reads p.kvb
        result["recommendation_summary"] = info.get("recommendationMean")
        result["sector"] = info.get("sector", "")
        result["industry"] = info.get("industry", "")
        result["short_name"] = info.get("shortName", ticker_str)

        # Fallbacks for instruments without analyst coverage
        if not result["analyst_target_mean"]:
            if ticker_str in CONFIG["manual_targets"]["gold"]["tickers"]:
                _apply_gold_target(result, rates)
            else:
                etf = get_etf_implied_target(ticker_str)
                if etf:
                    result["etf_implied"] = etf
                    cp = result["current_price"]
                    if etf.get("implied_upside") is not None and cp:
                        raw_target = cp * (1 + etf["implied_upside"] / 100)
                        result["analyst_target_mean"] = round(convert_to_base(raw_target, ticker_ccy, rates), 2)
                    if etf.get("range_low") and etf.get("range_high") and cp:
                        if ticker_str in CONFIG["manual_targets"]["bitcoin"]["tickers"]:
                            # ranges are absolute BTC targets -> scale by upside instead
                            low = cp * (1 + etf["min_upside"] / 100)
                            high = cp * (1 + etf["max_upside"] / 100)
                        else:
                            factor = cp / 100
                            low = etf["range_low"] * factor
                            high = etf["range_high"] * factor
                        result["analyst_target_low"] = round(convert_to_base(low, ticker_ccy, rates), 2)
                        result["analyst_target_high"] = round(convert_to_base(high, ticker_ccy, rates), 2)
                    if etf["implied_upside"] > 15:
                        result["recommendation"] = "buy"
                    elif etf["implied_upside"] > 0:
                        result["recommendation"] = "overweight"
                    else:
                        result["recommendation"] = "hold"

    except Exception as e:
        result["error"] = str(e)
        import traceback
        traceback.print_exc()

    result = sanitize(result)
    # Only cache usable results — a Yahoo hiccup must not pin an empty row
    if result.get("current_price") and not result.get("error"):
        cache_set(f"stock_{ticker_str}", result)
    return result


def get_stock_data(ticker_str, pos_currency="USD", period="2y"):
    """Stale-while-revalidate wrapper around _assemble_stock."""
    key = f"stock_{ticker_str}"
    cached, stale = cache_get(key)
    if cached and not stale:
        return cached
    if cached and stale:
        with _cache_lock:
            spawn = key not in _refreshing
            if spawn:
                _refreshing.add(key)
        if spawn:
            threading.Thread(
                target=lambda: _assemble_stock(ticker_str, pos_currency, period),
                daemon=True).start()
        return cached
    return _assemble_stock(ticker_str, pos_currency, period)


def cache_peek(ticker_str):
    """Any cached result (even stale) without triggering network access."""
    val, _ = cache_get(f"stock_{ticker_str}", ttl=STALE_TTL, stale_ttl=STALE_TTL)
    return val
