"""
DiCarlo BX Scanner - Email notifier
===================================
Reads results/latest.json (or the dict run_scan() just returned) and sends:

  * EXIT alert       - a trade you hold (positions.json) hit its stop or its
                       weekly/monthly BX turned red. Outranks everything else:
                       the strategy has no profit target, so this is the ONLY
                       thing that ends a trade.
  * STOP RAISE       - the stop ladder (config.STOP_LADDER) moved a held
                       trade's stop up; the mail goes out as [להעלות סטופ]
                       so you move the broker stop to match.
  * STRONG BUY alert - the moment a *new* prime setup shows up
  * Daily digest     - once a day even when there is nothing, so that a quiet
                       inbox proves the scan ran instead of hiding that it broke
  * Failure alert    - when the scan itself crashed

Dedupe matters here: MAX_DAYS_SINCE_FLIP is 3, so the same prime setup stays
prime for 2-3 consecutive scans. notify_state.json remembers what was already
sent so one setup produces one alert, not three.

Standalone use:
    python notify.py           # normal (respects dedupe + once-a-day digest)
    python notify.py --force   # ignore dedupe/daily flags, send what's in latest.json
    python notify.py --dry     # render to results/notify_preview.html, send nothing
    python notify.py --test    # send a test mail and exit
"""

import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "notify_state.json")
CONFIG_FILE = os.path.join(BASE, "notify_config.json")
LATEST_FILE = os.path.join(BASE, "results", "latest.json")

DEFAULTS = {
    # --- transport ---
    # Elastic Email is what the pgishonim system already sends through, so the
    # credentials exist and the domain is already a verified sender.
    "smtp_host": "smtp.elasticemail.com",
    "smtp_port": 2525,
    "smtp_security": "starttls",      # "starttls" | "ssl" | "none"
    "smtp_user": "",                  # blank -> pulled from env_file below
    "smtp_password": "",
    "smtp_timeout": 30,

    # Credentials are never stored here. Point this at an existing .env and the
    # notifier reads the two keys out of it at send time.
    "env_file": "",
    "env_user_key": "SMTP_USERNAME",
    "env_password_key": "SMTP_PASSWORD",

    # --- addressing ---
    "mail_from": "noreply@pgishonim.com",
    "mail_from_name": "DiCarlo BX Scanner",
    "mail_to": [],

    # --- behaviour ---
    "daily_digest": True,     # send a summary even on days with zero strong buys
    "realert_days": 5,        # don't re-alert the same ticker within N days
    "stale_hours": 20,        # flag the mail if latest.json is older than this
    "max_enter_rows": 25,     # cap the ENTER table so a wide day can't bloat the mail
    "dashboard_url": "http://localhost:5555",
}

# Env vars win over notify_config.json, so a scheduled task can be reconfigured
# without touching files.
ENV_OVERRIDES = {
    "BX_SMTP_HOST": ("smtp_host", str),
    "BX_SMTP_PORT": ("smtp_port", int),
    "BX_SMTP_SECURITY": ("smtp_security", str),
    "BX_SMTP_USER": ("smtp_user", str),
    "BX_SMTP_PASSWORD": ("smtp_password", str),
    "BX_MAIL_FROM": ("mail_from", str),
    "BX_MAIL_TO": ("mail_to", "csv"),
    "BX_ENV_FILE": ("env_file", str),
}


# ============================================================
# CONFIG
# ============================================================

def load_config():
    cfg = dict(DEFAULTS)

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user_cfg = json.load(f)
            if not isinstance(user_cfg, dict):
                raise ValueError("notify_config.json must contain a JSON object")
            cfg.update({k: v for k, v in user_cfg.items() if v not in ("", None)})
        except Exception as e:
            raise RuntimeError(f"bad notify_config.json: {e}")

    for env_key, (cfg_key, kind) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw in (None, ""):
            continue
        if kind == "csv":
            cfg[cfg_key] = [x.strip() for x in raw.split(",") if x.strip()]
        elif kind is int:
            cfg[cfg_key] = int(raw)
        else:
            cfg[cfg_key] = raw

    if isinstance(cfg["mail_to"], str):
        cfg["mail_to"] = [x.strip() for x in cfg["mail_to"].split(",") if x.strip()]

    # Fill credentials from the external .env only if they weren't given directly.
    if (not cfg["smtp_user"] or not cfg["smtp_password"]) and cfg["env_file"]:
        env = parse_env_file(cfg["env_file"])
        if not cfg["smtp_user"]:
            cfg["smtp_user"] = env.get(cfg["env_user_key"], "")
        if not cfg["smtp_password"]:
            cfg["smtp_password"] = env.get(cfg["env_password_key"], "")

    if not cfg["mail_to"]:
        raise RuntimeError("no recipients - set mail_to in notify_config.json")
    if not cfg["smtp_user"] or not cfg["smtp_password"]:
        raise RuntimeError(
            "no SMTP credentials - set smtp_user/smtp_password, or point "
            "env_file at a .env holding them"
        )
    return cfg


def parse_env_file(path):
    """Minimal .env reader: KEY=value, optional quotes, # comments, export prefix."""
    values = {}
    if not os.path.exists(path):
        raise RuntimeError(f"env_file not found: {path}")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            values[key] = val
    return values


# ============================================================
# STATE
# ============================================================

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError
    except Exception:
        state = {}
    state.setdefault("alerted", {})       # ticker -> ISO date of last alert
    state.setdefault("exit_alerted", {})  # ticker -> ISO date of last exit alert
    state.setdefault("raise_alerted", {})  # ticker -> ISO date of last stop-raise alert
    state.setdefault("last_digest", "")   # ISO date
    state.setdefault("last_error", "")    # ISO date, throttles crash mails
    return state


def save_state(state):
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    state["alerted"] = {t: d for t, d in state["alerted"].items() if d >= cutoff}
    state["exit_alerted"] = {t: d for t, d in state.get("exit_alerted", {}).items()
                             if d >= cutoff}
    state["raise_alerted"] = {t: d for t, d in state.get("raise_alerted", {}).items()
                              if d >= cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ============================================================
# FORMATTING
# ============================================================

def _num(v, fmt="{:.2f}", dash="—"):
    if v is None:
        return dash
    try:
        return fmt.format(v)
    except (TypeError, ValueError):
        return str(v)


def _earnings_txt(r):
    if not r.get("earnings_known"):
        return "לא ידוע"
    d = r.get("earnings_days")
    if d is None:
        return "לא ידוע"
    if d < 0:
        return "היום"
    return f"{d:.0f} ימים"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


CSS_TD = "padding:6px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;"
CSS_TH = ("padding:6px 10px;border-bottom:2px solid #d1d5db;font-size:12px;"
          "text-align:left;color:#6b7280;font-weight:600;white-space:nowrap;")


def _prime_card(r):
    bt = r.get("backtest") or {}
    # target_50/profit_50 are the pre-25% key names - still read them so a
    # latest.json written by an older scan renders instead of showing dashes.
    tgt_pct = r.get("target_pct", 50)
    tgt_price = r.get("target_price", r.get("target_50"))
    tgt_profit = r.get("target_profit", r.get("profit_50"))
    rows = [
        ("כניסה", f"${_num(r.get('price'))}"),
        ("סטופ", f"${_num(r.get('stop'))}  ({_num(r.get('stop_pct'), '{:.1f}')}%)  [{r.get('method', '')}]"),
        (f"יעד {_num(tgt_pct, '{:.0f}')}%",
         f"${_num(tgt_price)}  (רווח ${_num(tgt_profit)})  —  לתצוגה בלבד"),
        ("כמות", f"{r.get('shares', '—')} מניות  =  ${_num(r.get('cost'), '{:.0f}')}"),
        ("סיכון", f"${_num(r.get('risk'), '{:.0f}')}  ({_num(r.get('risk_pct'), '{:.1f}')}% מהתיק)   R:R {_num(r.get('rr'), '{:.1f}')}"),
        ("בקטסט", f"{bt.get('verdict', '—')} · score {r.get('score', '—')} · "
                  f"{bt.get('trades', '—')} עסקאות · win {_num(bt.get('win_rate'), '{:.1f}')}% · "
                  f"PF {_num(bt.get('profit_factor'), '{:.2f}')}"),
        ("BX", f"חודשי {_num(r.get('bx_m'))} ({r.get('bx_m_label', '')}) · "
               f"שבועי {_num(r.get('bx_w'))} ({r.get('bx_w_label', '')}) · "
               f"יומי {_num(r.get('bx_d'))} ({r.get('bx_d_label', '')})"),
        ("נפח / flip", f"{_num(r.get('vol_ratio'), '{:.2f}')}x · flip לפני {r.get('flip_days', '—')} ימים"),
        ("דוחות", _earnings_txt(r)),
    ]
    body = "".join(
        f'<tr><td style="{CSS_TD}color:#6b7280;white-space:nowrap;">{_esc(k)}</td>'
        f'<td style="{CSS_TD}" dir="ltr">{_esc(v)}</td></tr>'
        for k, v in rows
    )
    return (
        '<div style="border:2px solid #16a34a;border-radius:8px;margin:0 0 18px 0;overflow:hidden;">'
        '<div style="background:#16a34a;color:#fff;padding:10px 14px;font-size:18px;font-weight:700;" dir="ltr">'
        f'{_esc(r.get("ticker"))} &nbsp;<span style="font-size:13px;font-weight:400;">STRONG BUY</span></div>'
        f'<table style="width:100%;border-collapse:collapse;">{body}</table>'
        '</div>'
    )


def _enter_table(rows):
    if not rows:
        return ""
    head = "".join(f'<th style="{CSS_TH}">{h}</th>' for h in
                   ["", "Ticker", "Price", "Score", "Backtest", "T", "Win%", "PF",
                    "Shares", "Cost", "Stop", "Stop%", "R:R", "Earnings"])
    body = []
    for r in rows:
        bt = r.get("backtest") or {}
        star = "★" if r.get("prime") else ""
        bg = "background:#f0fdf4;" if r.get("prime") else ""
        cells = [
            star,
            r.get("ticker", ""),
            f"${_num(r.get('price'))}",
            r.get("score") if r.get("score") is not None else "—",
            bt.get("verdict", "—"),
            bt.get("trades", "—"),
            _num(bt.get("win_rate"), "{:.0f}"),
            _num(bt.get("profit_factor"), "{:.2f}"),
            r.get("shares", "—"),
            f"${_num(r.get('cost'), '{:.0f}')}",
            f"${_num(r.get('stop'))}",
            _num(r.get("stop_pct"), "{:.1f}"),
            _num(r.get("rr"), "{:.1f}"),
            _earnings_txt(r),
        ]
        body.append(f'<tr style="{bg}">' + "".join(
            f'<td style="{CSS_TD}">{_esc(c)}</td>' for c in cells) + "</tr>")
    return (
        '<div style="overflow-x:auto;">'
        '<table style="width:100%;border-collapse:collapse;" dir="ltr">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


# --- open positions -------------------------------------------------
# scanner.evaluate_position() emits structured reason codes, not sentences,
# so the mail can phrase them in Hebrew while the dashboard uses English.

POS_ACTION = {
    "EXIT":    ("יציאה", "#dc2626", "#fef2f2"),
    "WATCH":   ("מעקב", "#d97706", "#fffbeb"),
    "HOLD":    ("מחזיקים", "#16a34a", "#f0fdf4"),
    "NO DATA": ("לא נבדק", "#6b7280", "#f9fafb"),
}


def _reason_he(reason):
    reason = reason or {}
    code = reason.get("code")
    v = _num(reason.get("value")) if reason.get("value") is not None else ""
    if code == "stop_hit":
        return (f"הסטופ נפגע ב-{reason.get('date')} — הנמוך ירד ל-${_num(reason.get('low'))} "
                f"מול סטופ ${_num(reason.get('stop'))}")
    if code == "stop_raised":
        return (f"הסטופ הועלה מ-${_num(reason.get('from'))} ל-${_num(reason.get('to'))} — "
                f"המניה נגעה ב-+{_num(reason.get('trigger'), '{:.0f}')}% מהכניסה, "
                f"והכלל הוא סטופ ב-+{_num(reason.get('lock'), '{:.0f}')}%. לעדכן את הסטופ אצל הברוקר.")
    if code == "weekly_red":
        return f"ה-BX השבועי נסגר אדום ({v}) — זה איתות היציאה של האסטרטגיה"
    if code == "monthly_red":
        return f"ה-BX החודשי נסגר אדום ({v}) — זה איתות היציאה של האסטרטגיה"
    if code == "weekly_red_forming":
        return f"ה-BX השבועי כבר אדום ({v}) אבל השבוע עוד לא נסגר — לעקוב"
    if code == "monthly_red_forming":
        return f"ה-BX החודשי כבר אדום ({v}) אבל החודש עוד לא נסגר — לעקוב"
    if code == "daily_red":
        return f"היומי אדום ({v}) אבל שבועי וחודשי ירוקים — ממשיכים להחזיק"
    if code == "all_green":
        return "שלושת הטיים-פריימים ירוקים"
    if code == "no_data":
        return "אין מספיק נתונים לבדיקה"
    if code == "error":
        return f"שגיאה בבדיקה: {reason.get('text', '')}"
    return str(code or "")


def _position_box(p):
    label, colour, bg = POS_ACTION.get(p.get("action"), POS_ACTION["NO DATA"])
    pnl = p.get("pnl")
    if pnl is None:
        pnl_txt = "—"
    else:
        pct = p.get("pnl_pct") or 0
        pnl_txt = (f"{'+' if pnl >= 0 else ''}${_num(pnl, '{:.0f}')} "
                   f"({'+' if pct >= 0 else ''}{_num(pct, '{:.1f}')}%)")

    facts = (f"כניסה ${_num(p.get('entry_price'))} ({_esc(p.get('entry_date', ''))}) · "
             f"עכשיו ${_num(p.get('price'))} · "
             f"{p.get('shares', '—')} מניות · "
             f"סטופ ${_num(p.get('stop'))} · "
             f"{p.get('days_held', '—')} ימים")
    bx = (f"BX monthly {_num(p.get('bx_m'))} · weekly {_num(p.get('bx_w'))} · "
          f"daily {_num(p.get('bx_d'))}")
    bullets = "".join(f'<li style="margin:2px 0;">{_esc(_reason_he(r))}</li>'
                      for r in (p.get("reasons") or []))
    note_html = (f'<div style="color:#374151;font-size:12px;margin-top:3px;">&#128204; {_esc(p.get("note"))}</div>'
                 if p.get("note") else "")

    return (
        f'<div style="border:2px solid {colour};border-radius:8px;margin:0 0 10px 0;'
        f'overflow:hidden;background:{bg};">'
        f'<div style="background:{colour};color:#fff;padding:8px 12px;font-size:16px;'
        f'font-weight:700;">{_esc(label)} &nbsp;<span dir="ltr">{_esc(p.get("ticker"))}</span>'
        f'<span style="float:left;font-size:14px;" dir="ltr">{_esc(pnl_txt)}</span></div>'
        f'<div style="padding:8px 12px;font-size:13px;">'
        f'<div style="color:#4b5563;">{facts}</div>{note_html}'
        f'<div style="color:#6b7280;font-size:12px;margin-top:2px;" dir="ltr">{_esc(bx)}</div>'
        f'<ul style="margin:6px 0 0 0;padding-inline-start:18px;color:#111827;">{bullets}</ul>'
        f'</div></div>'
    )


def _positions_block(positions):
    if not positions:
        return ('<div style="color:#9ca3af;font-size:12px;margin:0 0 16px 0;">'
                'לא רשומות פוזיציות פתוחות — אפשר לרשום כניסה בדשבורד.</div>')

    exits = [p for p in positions if p.get("action") == "EXIT"]
    raised = [p for p in positions
              if any((r or {}).get("code") == "stop_raised" for r in (p.get("reasons") or []))]
    if exits:
        what = ('מפוזיציה אחת' if len(exits) == 1
                else f'מ-{len(exits)} פוזיציות')
        head = ('<h2 style="margin:0 0 8px 0;font-size:22px;color:#dc2626;">'
                f'יש לצאת {what}</h2>')
    elif raised:
        head = ('<h2 style="margin:0 0 8px 0;font-size:22px;color:#d97706;">'
                f'להעלות סטופ: {_esc(", ".join(p.get("ticker", "") for p in raised))}</h2>')
    else:
        head = ('<h3 style="margin:0 0 8px 0;font-size:16px;">'
                f'פוזיציות פתוחות ({len(positions)}) — אין איתות יציאה</h3>')
    return head + "".join(_position_box(p) for p in positions) + '<div style="height:10px;"></div>'


def _positions_text(positions):
    if not positions:
        return []
    lines = ["=== פוזיציות פתוחות ==="]
    for p in positions:
        label = POS_ACTION.get(p.get("action"), POS_ACTION["NO DATA"])[0]
        lines.append(f"  [{label}] {p.get('ticker')} @ ${_num(p.get('entry_price'))} "
                     f"-> ${_num(p.get('price'))} | P&L ${_num(p.get('pnl'), '{:.0f}')} "
                     f"({_num(p.get('pnl_pct'), '{:.1f}')}%)")
        for r in (p.get("reasons") or []):
            lines.append(f"      - {_reason_he(r)}")
    lines.append("")
    return lines


def build_html(data, new_primes, enters, positions=None, stale_hours=None, cfg=None):
    cfg = cfg or DEFAULTS
    positions = positions or []
    scan_time = data.get("scan_time", "?")
    parts = ['<div style="font-family:Segoe UI,Arial,sans-serif;color:#111827;'
             'max-width:760px;margin:0 auto;" dir="rtl">']

    if stale_hours is not None:
        parts.append(
            '<div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:6px;'
            'padding:10px 14px;margin-bottom:16px;font-size:14px;">'
            f'<b>שים לב:</b> הנתונים האלה מהסריקה של {_esc(scan_time)} — '
            f'לפני {stale_hours:.0f} שעות. ייתכן שהסריקה של היום לא רצה.</div>')

    parts.append(_positions_block(positions))

    if new_primes:
        parts.append(
            f'<h2 style="margin:0 0 4px 0;font-size:22px;color:#16a34a;">'
            f'{len(new_primes)} STRONG BUY חדשים</h2>')
    else:
        parts.append('<h2 style="margin:0 0 4px 0;font-size:22px;">סריקה יומית — אין STRONG BUY</h2>')

    parts.append(
        f'<div style="color:#6b7280;font-size:13px;margin-bottom:18px;">'
        f'סריקה: {_esc(scan_time)} · נותחו {data.get("total_analyzed", "?")} מניות '
        f'מתוך {data.get("total_scanned", "?")} · שגיאות {data.get("errors", 0)}</div>')

    if new_primes:
        parts.extend(_prime_card(r) for r in new_primes)

    fresh = {r["ticker"] for r in new_primes}
    already = [r for r in enters if r.get("prime") and r["ticker"] not in fresh]
    if already:
        parts.append(
            '<div style="color:#6b7280;font-size:13px;margin:0 0 14px 0;">'
            f'({len(already)} STRONG BUY נוספים עדיין פעילים אבל כבר דווחו: '
            f'{_esc(", ".join(r["ticker"] for r in already))})</div>')

    parts.append('<h3 style="margin:22px 0 8px 0;font-size:16px;">'
                 f'ENTER ({len(enters)})</h3>')
    if enters:
        shown = enters[:cfg.get("max_enter_rows", 25)]
        parts.append(_enter_table(shown))
        if len(enters) > len(shown):
            parts.append(f'<div style="color:#6b7280;font-size:12px;margin-top:6px;">'
                         f'מוצגות {len(shown)} מתוך {len(enters)} — השאר בדשבורד.</div>')
    else:
        parts.append('<div style="color:#6b7280;font-size:14px;">אין setups במצב ENTER היום.</div>')

    counts = (f'ALMOST {data.get("almost_count", 0)} · '
              f'WAIT DAILY {data.get("wait_daily_count", 0)} · '
              f'WATCH {data.get("watch_count", 0)} · '
              f'EARNINGS BLOCK {data.get("earnings_blocked", 0)}')
    parts.append(f'<div style="color:#6b7280;font-size:13px;margin-top:18px;">{counts}</div>')
    parts.append(
        '<div style="border-top:1px solid #e5e7eb;margin-top:20px;padding-top:12px;'
        'color:#9ca3af;font-size:12px;">'
        f'DiCarlo BX Scanner · דשבורד: {_esc(cfg.get("dashboard_url", ""))}</div>')
    parts.append("</div>")
    return "".join(parts)


def build_text(data, new_primes, enters, positions=None):
    lines = [f"סריקה: {data.get('scan_time', '?')}",
             f"נותחו {data.get('total_analyzed', '?')} מניות · "
             f"STRONG BUY {data.get('prime_count', 0)} · ENTER {data.get('enter_count', 0)}", ""]
    lines.extend(_positions_text(positions or []))
    if new_primes:
        lines.append(f"=== {len(new_primes)} STRONG BUY חדשים ===")
        for r in new_primes:
            bt = r.get("backtest") or {}
            lines.append(
                f"  {r.get('ticker')} @ ${_num(r.get('price'))} | stop ${_num(r.get('stop'))} "
                f"({_num(r.get('stop_pct'), '{:.1f}')}%) | {r.get('shares')} sh = "
                f"${_num(r.get('cost'), '{:.0f}')} | score {r.get('score')} "
                f"{bt.get('verdict', '')} {bt.get('trades', '')}T")
    else:
        lines.append("אין STRONG BUY חדשים.")
    lines.append("")
    lines.append(f"ENTER ({len(enters)}):")
    for r in enters:
        lines.append(f"  {'*' if r.get('prime') else ' '} {r.get('ticker')} "
                     f"@ ${_num(r.get('price'))} score {r.get('score')}")
    return "\n".join(lines)


# ============================================================
# SEND
# ============================================================

def send_mail(cfg, subject, html, text):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["mail_from_name"], cfg["mail_from"]))
    msg["To"] = ", ".join(cfg["mail_to"])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text or "")
    msg.add_alternative(html, subtype="html")

    host, port = cfg["smtp_host"], int(cfg["smtp_port"])
    security = (cfg.get("smtp_security") or "starttls").lower()
    timeout = int(cfg.get("smtp_timeout", 30))

    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        server.ehlo()
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


# ============================================================
# MAIN ENTRY
# ============================================================

def notify(data=None, force=False, dry_run=False):
    """Decide what (if anything) to mail. Returns a short status string.

    Never raises on a mail problem when called from the scanner - the caller
    wraps it - but does raise on a broken config so `python notify.py` tells
    you exactly what's wrong. dry_run renders the mail to
    results/notify_preview.html and touches neither SMTP nor the state file.
    """
    cfg = load_config()

    if data is None:
        with open(LATEST_FILE, encoding="utf-8") as f:
            data = json.load(f)

    results = data.get("results", []) or []
    enters = [r for r in results if r.get("status") == "ENTER"]
    primes = [r for r in results if r.get("prime")]
    positions = data.get("positions", []) or []
    exits = [p for p in positions if p.get("action") == "EXIT"]
    raised = [p for p in positions
              if any((r or {}).get("code") == "stop_raised" for r in (p.get("reasons") or []))]

    # Staleness: a scan that didn't run today must not read as "no opportunities".
    stale_hours = None
    try:
        scanned_at = datetime.strptime(data.get("scan_time", ""), "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - scanned_at).total_seconds() / 3600.0
        if age > float(cfg["stale_hours"]):
            stale_hours = age
    except (ValueError, TypeError):
        stale_hours = None

    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=int(cfg["realert_days"]))).strftime("%Y-%m-%d")

    if force:
        new_primes = primes
    else:
        new_primes = [r for r in primes
                      if state["alerted"].get(r["ticker"], "") < cutoff]

    digest_due = bool(cfg["daily_digest"]) and (force or state["last_digest"] != today)

    # An exit signal must get out even when the digest already went today -
    # but only once per ticker per day, so a re-run can't spam.
    if force:
        new_exits = exits
        new_raised = raised
    else:
        new_exits = [p for p in exits
                     if state["exit_alerted"].get(p["ticker"], "") != today]
        new_raised = [p for p in raised
                      if state["raise_alerted"].get(p["ticker"], "") != today]

    if not new_primes and not digest_due and not new_exits and not new_raised:
        return "nothing to send (no new strong buy, no new exit, no raised stop, digest already sent today)"

    if exits:
        ex = [p["ticker"] for p in exits]
        ex_head = ", ".join(ex[:3]) + (f" +{len(ex) - 3}" if len(ex) > 3 else "")
        subject = f"[יציאה] {ex_head}"
        if new_primes:
            subject += f" · STRONG BUY {len(new_primes)}"
    elif raised:
        names = [p["ticker"] for p in raised]
        head = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        subject = f"[להעלות סטופ] {head}"
        if new_primes:
            subject += f" · STRONG BUY {len(new_primes)}"
    elif new_primes:
        names = [r["ticker"] for r in new_primes]
        head = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        subject = f"[STRONG BUY] {head}"
    else:
        subject = f"סריקה יומית — אין STRONG BUY ({len(enters)} ENTER)"
    if stale_hours is not None:
        subject = "[נתונים ישנים] " + subject

    html = build_html(data, new_primes, enters, positions, stale_hours, cfg)
    text = build_text(data, new_primes, enters, positions)

    if dry_run:
        preview = os.path.join(BASE, "results", "notify_preview.html")
        os.makedirs(os.path.dirname(preview), exist_ok=True)
        with open(preview, "w", encoding="utf-8") as f:
            f.write(html)
        return f"DRY RUN — subject: {subject}  |  preview: {preview}"

    send_mail(cfg, subject, html, text)

    for r in new_primes:
        state["alerted"][r["ticker"]] = today
    for p in exits:
        state["exit_alerted"][p["ticker"]] = today
    for p in raised:
        state["raise_alerted"][p["ticker"]] = today
    if digest_due or new_primes or new_exits or new_raised:
        state["last_digest"] = today
    save_state(state)

    return f"sent: {subject}"


def notify_failure(error_text):
    """Mail a crash. Throttled to once a day so a broken scan can't spam."""
    cfg = load_config()
    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_error") == today:
        return "failure mail already sent today"

    html = ('<div style="font-family:Segoe UI,Arial,sans-serif;" dir="rtl">'
            '<h2 style="color:#dc2626;">הסריקה נכשלה</h2>'
            f'<div style="color:#6b7280;font-size:13px;">'
            f'{_esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</div>'
            '<pre style="background:#f3f4f6;padding:12px;border-radius:6px;'
            'font-size:12px;overflow-x:auto;white-space:pre-wrap;" dir="ltr">'
            f'{_esc(error_text)}</pre></div>')
    send_mail(cfg, "[שגיאה] הסריקה נכשלה — DiCarlo BX", html,
              f"הסריקה נכשלה:\n\n{error_text}")

    state["last_error"] = today
    save_state(state)
    return "failure mail sent"


def send_test():
    cfg = load_config()
    html = ('<div style="font-family:Segoe UI,Arial,sans-serif;" dir="rtl">'
            '<h2>בדיקת חיבור — DiCarlo BX Scanner</h2>'
            '<p>אם קיבלת את המייל הזה, ההתראות מוגדרות נכון.</p>'
            f'<div style="color:#6b7280;font-size:13px;" dir="ltr">'
            f'{_esc(cfg["smtp_host"])}:{cfg["smtp_port"]} · '
            f'from {_esc(cfg["mail_from"])} · to {_esc(", ".join(cfg["mail_to"]))}</div></div>')
    send_mail(cfg, "בדיקה — DiCarlo BX Scanner", html,
              "אם קיבלת את המייל הזה, ההתראות מוגדרות נכון.")
    return f"test mail sent to {', '.join(cfg['mail_to'])}"


def main():
    args = sys.argv[1:]
    try:
        if "--test" in args:
            print(send_test())
        else:
            print(notify(force="--force" in args, dry_run="--dry" in args))
    except Exception as e:
        print(f"notify failed: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
