"""
DiCarlo BX Scanner v1.0
Scans S&P 500 + NASDAQ 100 for BX Trender entry signals.
Replicates the Pine Script v5.2.1 logic in Python.
"""

import pandas as pd
import numpy as np
import yfinance as yf
import json
import math
import os
import sys
import time
from datetime import datetime

from config import *

# Make console output robust to non-ASCII (Windows cp1252 would otherwise
# crash on characters like emojis/stars mid-scan).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ============================================================
# TECHNICAL INDICATORS (match TradingView calculations)
# ============================================================

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def bx_trender(close_series, l1=5, l2=20, l3=15):
    ema_fast = ema(close_series, l1)
    ema_slow = ema(close_series, l2)
    short_term = ema_fast - ema_slow
    return rsi(short_term, l3) - 50.0


def calc_atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def bars_since_fresh_flip(bx, noise_floor):
    """Bars since the daily BX left CLEARLY-red territory (the start of the
    current up-move). Ignores shallow zero-line jitter so a brief dip to -0.5
    does not reset the 'days since flip' clock. Returns 999 if currently red
    or no recent red found.
    Matches TradingView's intent better than a naive crossover, which counts
    every micro-wiggle through zero as a brand-new flip."""
    n = len(bx)
    if n == 0 or bx[-1] <= 0:
        return 999
    for k in range(1, min(60, n)):
        if bx[-k] <= noise_floor:
            return k - 1
    return 999


def json_safe(obj):
    """Recursively replace NaN/Inf floats with None so the written JSON is
    valid (json.dump emits bare NaN/Infinity, which breaks JSON.parse in the
    browser's /api/results fetch)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def session_forming():
    """True if the current US daily bar is still forming (weekday, before the
    16:00 ET close) - in that case the latest downloaded daily bar is partial
    and must be dropped so signals are computed only on confirmed bars."""
    now = pd.Timestamp.now(tz="America/New_York")
    if now.weekday() >= 5:      # Sat/Sun: last bar is Friday (complete)
        return False
    return now.hour < 16        # before the 16:00 ET close


# ============================================================
# STOCK UNIVERSE
# ============================================================

def _fetch_wiki_table(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req).read().decode("utf-8")
    return pd.read_html(html)


def get_sp500_tickers():
    tables = _fetch_wiki_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    return tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()


def get_nasdaq100_tickers():
    tables = _fetch_wiki_table("https://en.wikipedia.org/wiki/Nasdaq-100")
    for table in tables:
        for col in ["Ticker", "Symbol"]:
            if col in table.columns:
                return table[col].str.replace(".", "-", regex=False).tolist()
    return []


def get_all_us_tickers():
    """All US-listed common stocks from NASDAQ Trader symbol directory
    (NASDAQ + NYSE + AMEX). Filters out test issues, ETFs, and
    non-common-stock symbols (warrants, units, preferreds)."""
    import urllib.request

    def fetch_lines(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        return text.splitlines()

    tickers = set()

    # --- NASDAQ-listed ---
    # Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    try:
        lines = fetch_lines("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt")
        for line in lines[1:]:
            if line.startswith("File Creation Time") or "|" not in line:
                continue
            f = line.split("|")
            if len(f) < 8:
                continue
            symbol, name, _, test_issue, _, _, etf = f[0], f[1], f[2], f[3], f[4], f[5], f[6]
            if test_issue == "Y":
                continue
            if EXCLUDE_ETFS and etf == "Y":
                continue
            if _is_common_stock(symbol, name):
                tickers.add(symbol.replace(".", "-"))
    except Exception as e:
        print(f"  Error fetching NASDAQ list: {e}")

    # --- Other (NYSE, AMEX, etc.) ---
    # ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    try:
        lines = fetch_lines("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
        for line in lines[1:]:
            if line.startswith("File Creation Time") or "|" not in line:
                continue
            f = line.split("|")
            if len(f) < 7:
                continue
            symbol, name, _, _, etf, _, test_issue = f[0], f[1], f[2], f[3], f[4], f[5], f[6]
            if test_issue == "Y":
                continue
            if EXCLUDE_ETFS and etf == "Y":
                continue
            if _is_common_stock(symbol, name):
                tickers.add(symbol.replace(".", "-"))
    except Exception as e:
        print(f"  Error fetching NYSE/AMEX list: {e}")

    return sorted(tickers)


def _is_common_stock(symbol, name):
    """Filter out warrants, units, preferred shares, rights, notes."""
    if not symbol or len(symbol) > 5:
        return False
    # Suffix characters that mark non-common issues
    if any(c in symbol for c in ["$", ".W", ".U", ".R", ".P"]):
        return False
    name_low = name.lower()
    junk = ["warrant", " unit", "preferred", "depositary", "right",
            "% note", "debenture", "subordinated"]
    if any(j in name_low for j in junk):
        return False
    return True


def get_tickers():
    tickers = set()

    if UNIVERSE in ("sp500", "sp500_nasdaq100"):
        try:
            sp = get_sp500_tickers()
            tickers.update(sp)
            print(f"  S&P 500: {len(sp)} tickers")
        except Exception as e:
            print(f"  Error fetching S&P 500: {e}")

    if UNIVERSE in ("nasdaq100", "sp500_nasdaq100"):
        try:
            nq = get_nasdaq100_tickers()
            before = len(tickers)
            tickers.update(nq)
            print(f"  NASDAQ 100: +{len(tickers) - before} new tickers")
        except Exception as e:
            print(f"  Error fetching NASDAQ 100: {e}")

    if UNIVERSE == "all_us":
        try:
            us = get_all_us_tickers()
            tickers.update(us)
            print(f"  All US stocks: {len(us)} tickers")
        except Exception as e:
            print(f"  Error fetching all US stocks: {e}")

    if UNIVERSE == "custom":
        pass  # only custom file

    custom_file = os.path.join(os.path.dirname(__file__), CUSTOM_TICKERS_FILE)
    if os.path.exists(custom_file):
        count = 0
        with open(custom_file) as f:
            for line in f:
                t = line.strip().upper()
                if t and not t.startswith("#"):
                    tickers.add(t)
                    count += 1
        if count:
            print(f"  Custom tickers: +{count}")

    return sorted(list(tickers))


# ============================================================
# DATA DOWNLOAD (with caching)
# ============================================================

def download_data(tickers):
    base = os.path.dirname(__file__)
    cache_file = os.path.join(base, "cache", "data.pkl")
    cache_meta = os.path.join(base, "cache", "meta.json")

    if os.path.exists(cache_file) and os.path.exists(cache_meta):
        with open(cache_meta) as f:
            meta = json.load(f)
        age_hours = (time.time() - meta.get("timestamp", 0)) / 3600
        if age_hours < CACHE_HOURS:
            print(f"  Using cached data ({age_hours:.1f}h old)")
            return pd.read_pickle(cache_file)

    print(f"  Downloading {len(tickers)} tickers ({MAIN_HISTORY_PERIOD} history)...")

    batch_size = DOWNLOAD_BATCH_SIZE if len(tickers) > DOWNLOAD_BATCH_SIZE else len(tickers)
    n_batches = (len(tickers) + batch_size - 1) // batch_size
    frames = []

    for b in range(n_batches):
        batch = tickers[b * batch_size:(b + 1) * batch_size]
        print(f"  Batch {b + 1}/{n_batches} ({len(batch)} tickers)...")
        try:
            part = yf.download(
                batch,
                period=MAIN_HISTORY_PERIOD,
                threads=True,
                progress=False,
            )
            if part is not None and not part.empty:
                # Single-ticker batches come back without the Ticker column level
                if not isinstance(part.columns, pd.MultiIndex):
                    part.columns = pd.MultiIndex.from_product([part.columns, [batch[0]]],
                                                              names=["Price", "Ticker"])
                frames.append(part)
        except Exception as e:
            print(f"    Batch {b + 1} error: {e}")

    if not frames:
        raise RuntimeError("No data downloaded")

    data = pd.concat(frames, axis=1)

    os.makedirs(os.path.join(base, "cache"), exist_ok=True)
    data.to_pickle(cache_file)
    with open(cache_meta, "w") as f:
        json.dump({"timestamp": time.time(), "tickers": tickers}, f)

    return data


# ============================================================
# STOCK ANALYSIS
# ============================================================

def analyze_stock(ticker, df):
    try:
        if df is None or len(df) < 60:
            return None

        df = df.dropna(subset=["Close"]).copy()
        if len(df) < 60:
            return None

        price = float(df["Close"].iloc[-1])
        if price <= 0 or np.isnan(price):
            return None

        # --- Liquidity gate (skip penny stocks & illiquid names) ---
        if price < MIN_PRICE:
            return None
        recent_vol = df["Volume"].iloc[-20:]
        avg_dollar_vol = float((df["Close"].iloc[-20:] * recent_vol).mean())
        if avg_dollar_vol < MIN_AVG_DOLLAR_VOLUME:
            return None

        # --- Daily BX ---
        df["bx"] = bx_trender(df["Close"], SHORT_L1, SHORT_L2, SHORT_L3)
        bx_d = float(df["bx"].iloc[-1])
        bx_d_prev = float(df["bx"].iloc[-2]) if len(df) > 1 else 0.0

        # --- Weekly BX ---
        weekly = (
            df.resample("W-FRI")
            .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
            .dropna(subset=["Close"])
        )
        if len(weekly) < 5:
            return None
        weekly["bx"] = bx_trender(weekly["Close"], SHORT_L1, SHORT_L2, SHORT_L3)
        bx_w = float(weekly["bx"].iloc[-1])
        bx_w_prev = float(weekly["bx"].iloc[-2]) if len(weekly) > 1 else 0.0

        # --- Monthly BX (month-end resample; converges with ~5y history) ---
        monthly = (
            df.resample("M")
            .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
            .dropna(subset=["Close"])
        )
        if len(monthly) < 3:
            return None
        monthly["bx"] = bx_trender(monthly["Close"], SHORT_L1, SHORT_L2, SHORT_L3)
        bx_m = float(monthly["bx"].iloc[-1])
        bx_m_prev = float(monthly["bx"].iloc[-2]) if len(monthly) > 1 else 0.0

        # Skip degenerate stocks whose BX is NaN (e.g. perfectly flat price over
        # the RSI window). NaN would otherwise break the JSON feed and the JS.
        if not (math.isfinite(bx_d) and math.isfinite(bx_w) and math.isfinite(bx_m)):
            return None

        # --- Filters ---
        m_green = bx_m > 0
        w_green = bx_w > 0
        d_green = bx_d > 0
        two_green = bx_d > 0 and bx_d_prev > 0

        # Deep red in lookback
        lookback = min(DEEP_RED_LOOKBACK, len(df))
        deep_red_min = float(df["bx"].iloc[-lookback:].min())
        deep_red_ok = deep_red_min < DEEP_RED_THRESHOLD if REQUIRE_DEEP_RED else True

        # Days since daily flip — robust to zero-line jitter (see helper)
        flip_bars = bars_since_fresh_flip(df["bx"].to_numpy(), FLIP_NOISE_FLOOR)
        flip_ok = flip_bars <= MAX_DAYS_SINCE_FLIP

        # Volume (previous completed day)
        vol_prev = float(df["Volume"].iloc[-2]) if len(df) > 1 else 0
        vol_window = df["Volume"].iloc[-22:-2] if len(df) > 22 else df["Volume"].iloc[:-1]
        vol_avg = float(vol_window.mean()) if len(vol_window) > 0 else 1.0
        vol_ratio = vol_prev / vol_avg if vol_avg > 0 else 0.0
        volume_ok = vol_ratio > VOLUME_MULT if REQUIRE_VOLUME else True

        # --- ATR & Stop ---
        atr_series = calc_atr(df["High"], df["Low"], df["Close"])
        atr_val = float(atr_series.iloc[-1])

        if STOP_METHOD == "ATR":
            stop_price = price - (atr_val * ATR_MULT)
        elif STOP_METHOD == "Recent Low":
            stop_price = float(df["Low"].iloc[-10:].min()) * 0.99
        else:
            stop_price = price * (1.0 - FIXED_STOP_PCT / 100.0)

        stop_pct = (1.0 - stop_price / price) * 100.0

        # --- Position Sizing (matches Pine Script exactly) ---
        risk_per_share = price - stop_price
        max_risk = ACCOUNT_SIZE * (MAX_RISK_PCT_OVERRIDE / 100.0)
        max_pos = ACCOUNT_SIZE * (MAX_POSITION_PCT / 100.0)
        target_pos = ACCOUNT_SIZE * (TARGET_POSITION_PCT / 100.0)

        shares = int(target_pos / price) if price > 0 else 0
        cost = shares * price
        method = "Target 33%"

        if cost < MIN_POSITION_SIZE and price > 0:
            min_sh = int(np.ceil(MIN_POSITION_SIZE / price))
            if min_sh * price <= max_pos and min_sh * risk_per_share <= max_risk:
                shares = min_sh
                method = "Bumped to Min"
            else:
                shares = 0
                method = "Too expensive"

        if shares > 0:
            max_sh_pos = int(max_pos / price)
            if shares > max_sh_pos:
                shares = max_sh_pos
                method = "Max 50% cap"
            if risk_per_share > 0:
                max_sh_risk = int(max_risk / risk_per_share)
                if shares > max_sh_risk:
                    shares = max_sh_risk
                    method = "Max 5% risk cap"

        cost = shares * price
        risk_amount = shares * risk_per_share + (COMMISSION_PER_TRADE * 2)
        risk_pct = (risk_amount / ACCOUNT_SIZE) * 100.0 if ACCOUNT_SIZE > 0 else 0
        too_small = shares == 0 or cost < MIN_POSITION_SIZE

        # Profit targets
        target_50_price = price * 1.5
        profit_50 = (target_50_price - price) * shares - (COMMISSION_PER_TRADE * 2)
        rr = profit_50 / risk_amount if risk_amount > 0 else 0

        # --- Status ---
        all_green = m_green and w_green and d_green
        flip_confirmed = two_green if REQUIRE_TWO_GREEN else d_green
        filters_pass = all_green and flip_confirmed and deep_red_ok and flip_ok and volume_ok and not too_small

        if filters_pass:
            status, priority = "ENTER", 1
        elif all_green and not flip_ok:
            status, priority = "TOO LATE", 3
        elif all_green:
            status, priority = "ALMOST", 2
        elif m_green and w_green:
            status, priority = "WAIT DAILY", 4
        elif m_green:
            status, priority = "WATCH", 5
        else:
            status, priority = "NO SETUP", 6

        def bx_label(v):
            if v > 10:
                return "STRONG"
            if v > 0:
                return "Green"
            if v > -10:
                return "Light Red"
            return "Deep Red"

        # Missing filters (for ALMOST/WAIT status)
        missing = []
        if not m_green:
            missing.append("Monthly red")
        if not w_green:
            missing.append("Weekly red")
        if not d_green:
            missing.append("Daily red")
        elif not flip_confirmed:
            missing.append("Need 2nd green day")
        if not deep_red_ok:
            missing.append("No deep red recovery")
        if not flip_ok:
            missing.append(f"Flip {flip_bars}d ago (max {MAX_DAYS_SINCE_FLIP})")
        if not volume_ok:
            missing.append(f"Volume {vol_ratio:.1f}x (need {VOLUME_MULT}x)")
        if too_small:
            missing.append("Position too small")

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "bx_m": round(bx_m, 2),
            "bx_m_delta": round(bx_m - bx_m_prev, 2),
            "bx_m_label": bx_label(bx_m),
            "bx_w": round(bx_w, 2),
            "bx_w_delta": round(bx_w - bx_w_prev, 2),
            "bx_w_label": bx_label(bx_w),
            "bx_d": round(bx_d, 2),
            "bx_d_delta": round(bx_d - bx_d_prev, 2),
            "bx_d_label": bx_label(bx_d),
            "vol_ratio": round(vol_ratio, 2),
            "vol_ok": volume_ok,
            "flip_days": flip_bars if flip_bars < 999 else None,
            "flip_ok": flip_ok,
            "deep_red_ok": deep_red_ok,
            "two_green": two_green,
            "shares": shares,
            "cost": round(cost, 2),
            "stop": round(stop_price, 2),
            "stop_pct": round(stop_pct, 1),
            "risk": round(risk_amount, 2),
            "risk_pct": round(risk_pct, 1),
            "method": method,
            "too_small": too_small,
            "target_50": round(target_50_price, 2),
            "profit_50": round(profit_50, 2),
            "rr": round(rr, 1),
            "atr": round(atr_val, 2),
            "status": status,
            "priority": priority,
            "m_green": m_green,
            "w_green": w_green,
            "d_green": d_green,
            "missing": missing,
        }
    except Exception:
        return None


# ============================================================
# POSITION SIZING HELPER (shared by live + backtest)
# ============================================================

def calc_position_shares(entry_price, stop_price):
    if entry_price <= 0:
        return 0
    risk_per_share = entry_price - stop_price
    max_risk = ACCOUNT_SIZE * (MAX_RISK_PCT_OVERRIDE / 100.0)
    max_pos = ACCOUNT_SIZE * (MAX_POSITION_PCT / 100.0)
    target_pos = ACCOUNT_SIZE * (TARGET_POSITION_PCT / 100.0)

    shares = int(target_pos / entry_price)
    if shares * entry_price < MIN_POSITION_SIZE:
        min_sh = int(np.ceil(MIN_POSITION_SIZE / entry_price))
        if min_sh * entry_price <= max_pos and min_sh * risk_per_share <= max_risk:
            shares = min_sh
        else:
            shares = 0
    if shares > 0:
        max_sh_pos = int(max_pos / entry_price)
        if shares > max_sh_pos:
            shares = max_sh_pos
        if risk_per_share > 0:
            max_sh_risk = int(max_risk / risk_per_share)
            if shares > max_sh_risk:
                shares = max_sh_risk
    return shares


# ============================================================
# BACKTEST ENGINE (replicates Pine Script v5.2.1 backtest)
# Walks full daily history, simulates entries/exits, scores quality.
# ============================================================

def run_backtest(df):
    try:
        df = df.dropna(subset=["Close"]).copy()
        if len(df) < 80:
            return None

        # Daily BX
        bx_d = bx_trender(df["Close"], SHORT_L1, SHORT_L2, SHORT_L3)

        # Weekly/Monthly BX aligned to daily — LOOKAHEAD-SAFE: each daily bar
        # uses only the PREVIOUS COMPLETED higher-TF bar (ffill from period-END
        # label). Mapping the period's final value back over its own days would
        # leak the future and turn losers into fake winners (e.g. SPHR showed
        # EXCELLENT instead of its real AVOID). Conservative but honest.
        weekly = (df.resample("W-FRI")
                  .agg({"Close": "last"}).dropna(subset=["Close"]))
        wk_bx = bx_trender(weekly["Close"], SHORT_L1, SHORT_L2, SHORT_L3)
        bx_w = wk_bx.reindex(df.index, method="ffill")

        monthly = (df.resample("M")
                   .agg({"Close": "last"}).dropna(subset=["Close"]))
        mo_bx = bx_trender(monthly["Close"], SHORT_L1, SHORT_L2, SHORT_L3)
        bx_m = mo_bx.reindex(df.index, method="ffill")

        atr = calc_atr(df["High"], df["Low"], df["Close"])
        deep_red_min = bx_d.rolling(DEEP_RED_LOOKBACK).min()
        vol_prev = df["Volume"].shift(1)
        vol_avg_prev = df["Volume"].rolling(20).mean().shift(1)

        # To numpy for speed
        close = df["Close"].to_numpy()
        low = df["Low"].to_numpy()
        d = bx_d.to_numpy()
        w = bx_w.to_numpy()
        m = bx_m.to_numpy()
        atr_a = atr.to_numpy()
        dr = deep_red_min.to_numpy()
        vp = vol_prev.to_numpy()
        va = vol_avg_prev.to_numpy()
        n = len(close)

        last_red_idx = -999
        in_trade = False
        entry_price = entry_stop = 0.0
        entry_shares = 0
        entry_bar = 0

        pnls = []
        holds = []
        stopped = []

        for i in range(1, n):
            # Track daily flip — robust to zero-line jitter (same as live)
            if d[i] <= FLIP_NOISE_FLOOR:
                last_red_idx = i
            bars_since_flip = (i - last_red_idx) if last_red_idx >= 0 else 999

            # Skip bars without valid higher-TF / indicator data
            if np.isnan(w[i]) or np.isnan(m[i]) or np.isnan(atr_a[i]) or np.isnan(dr[i]):
                continue

            # Exit handling first (if in a trade)
            if in_trade:
                if low[i] <= entry_stop:
                    pnl = (entry_stop - entry_price) * entry_shares - (COMMISSION_PER_TRADE * 2)
                    pnls.append(pnl); holds.append(i - entry_bar); stopped.append(True)
                    in_trade = False
                else:
                    wk_red = w[i] < 0 and w[i - 1] >= 0
                    mo_red = m[i] < 0 and m[i - 1] >= 0
                    if wk_red or mo_red:
                        pnl = (close[i] - entry_price) * entry_shares - (COMMISSION_PER_TRADE * 2)
                        pnls.append(pnl); holds.append(i - entry_bar); stopped.append(False)
                        in_trade = False

            # Entry handling
            if not in_trade:
                m_green = m[i] > 0
                w_green = w[i] > 0
                d_green = d[i] > 0
                flip_confirmed = (d[i] > 0 and d[i - 1] > 0) if REQUIRE_TWO_GREEN else d_green
                deep_red_ok = (dr[i] < DEEP_RED_THRESHOLD) if REQUIRE_DEEP_RED else True
                flip_ok = bars_since_flip <= MAX_DAYS_SINCE_FLIP
                vol_ok = True
                if REQUIRE_VOLUME:
                    vol_ok = (not np.isnan(vp[i]) and not np.isnan(va[i]) and va[i] > 0
                              and vp[i] / va[i] > VOLUME_MULT)

                if m_green and w_green and d_green and flip_confirmed and deep_red_ok and flip_ok and vol_ok:
                    if STOP_METHOD == "ATR":
                        stop = close[i] - atr_a[i] * ATR_MULT
                    elif STOP_METHOD == "Recent Low":
                        stop = float(df["Low"].iloc[max(0, i - 10):i + 1].min()) * 0.99
                    else:
                        stop = close[i] * (1 - FIXED_STOP_PCT / 100.0)
                    shares = calc_position_shares(close[i], stop)
                    if shares > 0:
                        entry_price = close[i]; entry_stop = stop
                        entry_shares = shares; entry_bar = i
                        in_trade = True

        return _backtest_stats(pnls, holds, stopped)
    except Exception:
        return None


def _backtest_stats(pnls, holds, stopped):
    total = len(pnls)
    if total == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "total_pnl": 0.0,
                "roi": 0.0, "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "stop_rate": 0.0, "avg_hold": 0, "score": 0, "verdict": "NO TRADES"}

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_win = sum(wins)
    total_loss = sum(losses)
    win_rate = len(wins) / total * 100.0
    total_pnl = sum(pnls)
    roi = total_pnl / ACCOUNT_SIZE * 100.0
    expectancy = total_pnl / total
    avg_win = (total_win / len(wins)) if wins else 0.0
    avg_loss = (total_loss / len(losses)) if losses else 0.0
    stop_rate = sum(1 for s in stopped if s) / total * 100.0
    avg_hold = int(sum(holds) / total) if holds else 0

    if total_loss != 0:
        profit_factor = abs(total_win / total_loss)
    elif total_win > 0:
        profit_factor = 99.0   # no losing trades
    else:
        profit_factor = 0.0

    # --- Verdict (same thresholds as Pine Script) ---
    if total < MIN_BACKTEST_TRADES:
        verdict = "NEED DATA"
    elif profit_factor >= 2 and win_rate >= 50:
        verdict = "EXCELLENT"
    elif profit_factor >= 1.5:
        verdict = "GOOD"
    elif profit_factor >= 1.0:
        verdict = "MARGINAL"
    else:
        verdict = "AVOID"

    # --- Composite score 0-100 (mainly backtest quality) ---
    pf_score = min(profit_factor / 3.0, 1.0) * 40      # profit factor (most weight)
    wr_score = min(win_rate / 60.0, 1.0) * 25          # win rate (60%+ = full)
    exp_pct = expectancy / ACCOUNT_SIZE * 100.0
    exp_score = max(0.0, min(exp_pct / 5.0, 1.0)) * 20  # expectancy per trade
    sample_score = min(total / 10.0, 1.0) * 15          # sample-size confidence
    score = pf_score + wr_score + exp_score + sample_score
    if total < MIN_BACKTEST_TRADES:
        score = score * 0.4   # heavily discount unreliable small samples

    return {
        "trades": total,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": round(roi, 1),
        "expectancy": round(expectancy, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "stop_rate": round(stop_rate, 1),
        "avg_hold": avg_hold,
        "score": round(score),
        "verdict": verdict,
    }


# ============================================================
# EARNINGS CHECK (only for ENTER/ALMOST stocks)
# ============================================================

def check_earnings_batch(tickers_to_check):
    """Returns days-until-next-earnings as a FLOAT (fractional days) per ticker,
    or None if unknown. Uses get_earnings_dates (which has the actual report
    TIME, e.g. 16:00) so 'earnings later today' is correctly seen as imminent,
    not as 0 days. The old code used the calendar DATE (midnight) + integer
    .days and missed same-day earnings entirely."""
    tz = "America/New_York"
    now = pd.Timestamp.now(tz=tz)
    results = {}
    for ticker in tickers_to_check:
        days = None
        # Preferred: precise earnings datetimes
        try:
            ed = yf.Ticker(ticker).get_earnings_dates(limit=16)
            if ed is not None and len(ed) > 0:
                future = []
                for d in ed.index:
                    dt = pd.Timestamp(d)
                    if dt.tz is None:
                        dt = dt.tz_localize(tz)
                    delta = (dt - now).total_seconds() / 86400.0
                    # keep anything from "earlier today" (>= -1) onward
                    if delta >= -1:
                        future.append(delta)
                if future:
                    days = min(future)
        except Exception:
            pass
        # Fallback: calendar (date only)
        if days is None:
            try:
                cal = yf.Ticker(ticker).calendar
                raw = None
                if isinstance(cal, dict) and cal.get("Earnings Date"):
                    raw = cal["Earnings Date"][0]
                elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.columns and len(cal):
                    raw = cal["Earnings Date"].iloc[0]
                if raw is not None:
                    dt = pd.Timestamp(raw)
                    if dt.tz is None:
                        dt = dt.tz_localize(tz)
                    days = (dt - now).total_seconds() / 86400.0
            except Exception:
                pass
        results[ticker] = days
    return results


# ============================================================
# MAIN SCAN
# ============================================================

def run_scan():
    print("=" * 60)
    print("  DiCarlo BX Scanner v1.0")
    print("=" * 60)

    # 1. Get tickers
    print("\n[1/5] Fetching stock universe...")
    tickers = get_tickers()
    print(f"  Total: {len(tickers)} tickers")

    if not tickers:
        print("  ERROR: No tickers found!")
        return None

    # 2. Download data
    print(f"\n[2/5] Downloading market data...")
    data = download_data(tickers)

    # Drop today's still-forming daily bar if the US session is open, so signals
    # use only confirmed bars (safe to scan any time of day).
    if session_forming() and len(data) > 1:
        last_date = pd.Timestamp(data.index[-1]).date()
        today_et = pd.Timestamp.now(tz="America/New_York").date()
        if last_date == today_et:
            data = data.iloc[:-1]
            print("  Dropped today's still-forming daily bar (using confirmed bars only).")

    # 3. Analyze each stock
    print(f"\n[3/5] Analyzing stocks...")
    results = []
    errors = 0
    single = len(tickers) == 1

    for idx, ticker in enumerate(tickers):
        if (idx + 1) % 50 == 0:
            print(f"  ... {idx + 1}/{len(tickers)}")

        try:
            if single:
                ticker_df = data.droplevel("Ticker", axis=1).copy()
            else:
                ticker_df = data.xs(ticker, level="Ticker", axis=1).copy()

            if ticker_df is None or ticker_df.empty:
                errors += 1
                continue

            ticker_df = ticker_df.dropna(how="all")
            if len(ticker_df) < 60:
                errors += 1
                continue

            result = analyze_stock(ticker, ticker_df)
            if result:
                results.append(result)
        except (KeyError, TypeError):
            errors += 1
            continue

    # 4. Earnings check for top candidates
    print(f"\n[4/5] Checking earnings for top candidates...")
    candidates = [r["ticker"] for r in results if r["status"] in ("ENTER", "ALMOST")]
    if candidates and BLOCK_EARNINGS:
        print(f"  Checking {len(candidates)} stocks...")
        earnings = check_earnings_batch(candidates)
        unknown = 0
        for r in results:
            if r["ticker"] in earnings:
                days = earnings[r["ticker"]]
                r["earnings_days"] = round(days) if days is not None else None
                # FAIL-SAFE: if the earnings date is unknown (data source failed),
                # do NOT treat it as safe. Mark it unknown so it can't become a
                # confident STRONG BUY, and warn the user to verify manually.
                r["earnings_known"] = days is not None
                if days is not None and -1 <= days <= EARNINGS_BUFFER_DAYS:
                    r["near_earnings"] = True
                    if r["status"] == "ENTER":
                        r["status"] = "EARNINGS BLOCK"
                        r["priority"] = 2
                        d_lbl = "today" if -1 <= days < 1 else f"in {round(days)} days"
                        r["missing"].append(f"Earnings {d_lbl}")
                else:
                    r["near_earnings"] = False
                    if not r["earnings_known"] and r["status"] == "ENTER":
                        unknown += 1
                        r["missing"].append("Earnings unknown - verify")
            else:
                r["earnings_days"] = None
                r["near_earnings"] = False
                r["earnings_known"] = False
        if unknown:
            print(f"  WARNING: earnings date unknown for {unknown} ENTER stock(s) "
                  f"(data source failed) - they will NOT be marked STRONG BUY.")
    else:
        # Earnings filter disabled by config -> treat as known/not-applicable.
        for r in results:
            r["earnings_days"] = None
            r["near_earnings"] = False
            r["earnings_known"] = not BLOCK_EARNINGS

    # 5. Backtest actionable setups for scoring.
    # Downloads EXTENDED history (10y) for just the candidates - TradingView's
    # chart loads ~10y of bars, so this matches its trade counts/verdicts.
    # The strategy's rare entries need many years to form a meaningful sample.
    print(f"\n[5/5] Backtesting actionable setups for scoring...")
    bt_candidates = [r for r in results if r["status"] in BACKTEST_STATUSES] if RUN_BACKTEST else []
    print(f"  Backtesting {len(bt_candidates)} stocks ({BACKTEST_HISTORY_PERIOD} history)...")

    bt_data = None
    if bt_candidates:
        bt_tickers = [r["ticker"] for r in bt_candidates]
        try:
            bt_data = yf.download(bt_tickers, period=BACKTEST_HISTORY_PERIOD,
                                  threads=True, progress=False)
        except Exception as e:
            print(f"  Backtest history download error: {e}")

    for r in bt_candidates:
        bt = None
        try:
            if bt_data is not None and not bt_data.empty:
                if isinstance(bt_data.columns, pd.MultiIndex):
                    tdf = bt_data.xs(r["ticker"], level="Ticker", axis=1).dropna(how="all")
                else:
                    tdf = bt_data.dropna(how="all")
                bt = run_backtest(tdf)
        except (KeyError, TypeError):
            bt = None
        r["backtest"] = bt
        r["score"] = bt["score"] if bt else None
        r["bt_verdict"] = bt["verdict"] if bt else None

    for r in results:
        if "backtest" not in r:
            r["backtest"] = None
            r["score"] = None
            r["bt_verdict"] = None

    # PRIME / STRONG BUY = top verdict says ENTER **and** backtest is strongly
    # green (GOOD or EXCELLENT with a solid score). These are the "both green"
    # setups worth focusing the limited slots on.
    for r in results:
        bt = r.get("backtest")
        r["prime"] = bool(
            r["status"] == "ENTER" and bt
            and bt["verdict"] in ("GOOD", "EXCELLENT")
            and (r["score"] or 0) >= PRIME_MIN_SCORE
            and bt["trades"] >= PRIME_MIN_TRADES
            and r.get("earnings_known")          # fail-safe: never prime on unknown earnings
        )

    # Sort: prime first, then status priority, then backtest score, then daily BX
    results.sort(key=lambda x: (
        0 if x.get("prime") else 1,
        x["priority"],
        -(x["score"] if x["score"] is not None else -1),
        -x["bx_d"],
    ))

    # Summary
    enter = sum(1 for r in results if r["status"] == "ENTER")
    prime = sum(1 for r in results if r.get("prime"))
    almost = sum(1 for r in results if r["status"] == "ALMOST")
    wait = sum(1 for r in results if r["status"] == "WAIT DAILY")
    watch = sum(1 for r in results if r["status"] == "WATCH")
    earnings_blocked = sum(1 for r in results if r["status"] == "EARNINGS BLOCK")

    print(f"\n{'=' * 60}")
    print(f"  RESULTS")
    print(f"{'=' * 60}")
    print(f"  STRONG BUY:      {prime}  (ENTER + backtest GOOD/EXCELLENT)")
    print(f"  ENTER (total):   {enter}")
    if earnings_blocked:
        print(f"  EARNINGS BLOCK:  {earnings_blocked}")
    print(f"  ALMOST:          {almost}")
    print(f"  WAIT DAILY:      {wait}")
    print(f"  WATCH:           {watch}")
    print(f"  Total analyzed:  {len(results)} | Errors: {errors}")
    print(f"{'=' * 60}")

    if enter > 0:
        print(f"\n  SETUPS FOUND (* = STRONG BUY, ranked prime-first by score):")
        for r in results:
            if r["status"] == "ENTER":
                bt = r.get("backtest")
                score_txt = f"Score {r['score']:>3}" if r.get("score") is not None else "Score  — "
                bt_txt = (f"{bt['verdict']:9s} PF {bt['profit_factor']:>4.1f} | "
                          f"Win {bt['win_rate']:>4.1f}% | {bt['trades']:>2}T") if bt else "no backtest"
                star = "*" if r.get("prime") else " "
                print(
                    f"  {star} {score_txt} | {r['ticker']:6s} @ ${r['price']:>8.2f} | "
                    f"{bt_txt} | {r['shares']} sh (${r['cost']:.0f})"
                )

    # Save results
    output = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_scanned": len(tickers),
        "total_analyzed": len(results),
        "enter_count": enter,
        "prime_count": prime,
        "almost_count": almost,
        "wait_daily_count": wait,
        "watch_count": watch,
        "earnings_blocked": earnings_blocked,
        "errors": errors,
        "config": {
            "account_size": ACCOUNT_SIZE,
            "stop_method": STOP_METHOD,
            "atr_mult": ATR_MULT,
            "universe": UNIVERSE,
        },
        "results": results,
    }

    base = os.path.dirname(__file__)
    results_dir = os.path.join(base, "results")
    os.makedirs(results_dir, exist_ok=True)

    output = json_safe(output)   # strip any NaN/Inf so the JSON stays valid

    latest = os.path.join(results_dir, "latest.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2)

    dated = os.path.join(results_dir, f"scan_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(dated, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n  Saved: results/latest.json")
    print(f"  Archive: {os.path.basename(dated)}")
    print(f"\n  Open dashboard: http://localhost:{DASHBOARD_PORT}")

    return output


if __name__ == "__main__":
    try:
        output = run_scan()
    except Exception:
        # A crash must not fail silently - from the inbox side it looks exactly
        # like "no opportunities today", which is the one thing worse than noise.
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            import notify
            print(f"  [notify] {notify.notify_failure(tb)}")
        except Exception as e:
            print(f"  [notify] could not send failure mail: {e}")
        raise

    try:
        import notify
        print(f"\n  [notify] {notify.notify(output)}")
    except Exception as e:
        # A mail problem never invalidates a good scan.
        print(f"\n  [notify] skipped: {type(e).__name__}: {e}")
