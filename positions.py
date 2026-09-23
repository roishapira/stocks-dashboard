"""
DiCarlo BX Scanner - Open position store
========================================
The scanner only ever found ENTRIES. This file is the missing half: it
remembers which trades were actually taken, so the daily scan can tell you
when the strategy says to GET OUT.

Plain JSON list in positions.json - one record per trade:

    {"id": 3, "ticker": "TWLO", "entry_date": "2026-08-11",
     "entry_price": 104.2, "shares": 6, "stop": 94.8,
     "status": "open", "closed_date": null, "exit_price": null, "note": ""}

Deliberately dumb: no indicators, no yfinance, no scanner import - so both
dashboard.py (writes) and scanner.py (reads) can use it without a cycle.
The exit LOGIC lives in scanner.evaluate_position().
"""

import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
POSITIONS_FILE = os.path.join(BASE, "positions.json")


# ============================================================
# STORE
# ============================================================

def load_positions():
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError
    except Exception:
        return []
    return [p for p in data if isinstance(p, dict) and p.get("ticker")]


def save_positions(positions):
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2, ensure_ascii=False)
    os.replace(tmp, POSITIONS_FILE)


def open_positions(positions=None):
    if positions is None:
        positions = load_positions()
    return [p for p in positions if p.get("status") == "open"]


# ============================================================
# MUTATIONS
# ============================================================

def add_position(ticker, entry_price, shares, stop, entry_date=None, note="", stop_since=None):
    """Record a trade you just took. Raises ValueError with a Hebrew message
    on bad input - the dashboard shows it as-is.

    stop_since is the first day the recorded stop was in force. It defaults to
    the entry date when the trade is registered within 5 days of it (the stop
    was set at entry) and to today for an older trade registered late - so the
    exit check never tests a stop against days on which it did not exist yet."""
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise ValueError("חסר טיקר")

    try:
        entry_price = float(entry_price)
        shares = int(float(shares))
        stop = float(stop)
    except (TypeError, ValueError):
        raise ValueError("מחיר / כמות / סטופ חייבים להיות מספרים")

    if entry_price <= 0:
        raise ValueError("מחיר כניסה חייב להיות גדול מ-0")
    if shares < 1:
        raise ValueError("כמות מניות חייבת להיות לפחות 1")
    if stop <= 0:
        raise ValueError("סטופ חייב להיות גדול מ-0")
    entry_date = str(entry_date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("תאריך כניסה חייב להיות בפורמט YYYY-MM-DD")

    if stop >= entry_price and entry_date == datetime.now().strftime("%Y-%m-%d"):
        # Long-only strategy: a stop at or above entry on a trade opened TODAY
        # would fire immediately and is always a typo. An older trade may well
        # carry a stop above its entry - the stop ladder raises it there.
        raise ValueError("הסטופ חייב להיות נמוך ממחיר הכניסה")

    today = datetime.now().strftime("%Y-%m-%d")
    if stop_since:
        stop_since = str(stop_since).strip()
        try:
            datetime.strptime(stop_since, "%Y-%m-%d")
        except ValueError:
            raise ValueError("תאריך הסטופ חייב להיות בפורמט YYYY-MM-DD")
    else:
        age_days = (datetime.now() - datetime.strptime(entry_date, "%Y-%m-%d")).days
        stop_since = entry_date if age_days <= 5 else today

    positions = load_positions()
    if any(p.get("ticker") == ticker and p.get("status") == "open" for p in positions):
        raise ValueError(f"{ticker} כבר רשום כפוזיציה פתוחה")

    pos = {
        "id": max([int(p.get("id") or 0) for p in positions], default=0) + 1,
        "ticker": ticker,
        "entry_date": entry_date,
        "entry_price": round(entry_price, 4),
        "shares": shares,
        "stop": round(stop, 4),
        "stop_since": stop_since,
        "note": str(note or "")[:200],
        "status": "open",
        "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "closed_date": None,
        "exit_price": None,
    }
    positions.append(pos)
    save_positions(positions)
    return pos


def update_stop(pos_id, new_stop, why=""):
    """Raise (or set) the stop of an open trade. The daily check calls this
    when the stop ladder (config.STOP_LADDER) kicks in; every change is kept
    in the record's stop_log so you can see why the stop moved."""
    positions = load_positions()
    for p in positions:
        if int(p.get("id") or 0) == int(pos_id) and p.get("status") == "open":
            try:
                new_stop = round(float(new_stop), 4)
            except (TypeError, ValueError):
                raise ValueError("סטופ חייב להיות מספר")
            if new_stop <= 0:
                raise ValueError("סטופ חייב להיות גדול מ-0")
            p.setdefault("stop_log", []).append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "from": p.get("stop"), "to": new_stop, "why": str(why or "")[:120],
            })
            p["stop"] = new_stop
            save_positions(positions)
            return p
    raise ValueError("הפוזיציה לא נמצאה או שהיא כבר סגורה")


def close_position(pos_id, exit_price=None):
    """Mark a trade as closed so it stops showing up in the daily mail."""
    positions = load_positions()
    for p in positions:
        if int(p.get("id") or 0) == int(pos_id) and p.get("status") == "open":
            if exit_price not in (None, ""):
                try:
                    p["exit_price"] = round(float(exit_price), 4)
                except (TypeError, ValueError):
                    raise ValueError("מחיר יציאה חייב להיות מספר")
            p["status"] = "closed"
            p["closed_date"] = datetime.now().strftime("%Y-%m-%d")
            save_positions(positions)
            return p
    raise ValueError("הפוזיציה לא נמצאה או שהיא כבר סגורה")
