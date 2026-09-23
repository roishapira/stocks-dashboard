"""
DiCarlo BX Scanner - Web Dashboard
Run with: python dashboard.py
Open: http://localhost:5555
"""

from flask import Flask, render_template, jsonify, request
import json
import os
import subprocess
import sys

import positions as positions_store
from config import DASHBOARD_PORT

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
BASE = os.path.dirname(__file__)


def load_results():
    path = os.path.join(BASE, "results", "latest.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "results": [],
            "scan_time": "Never - run scanner first",
            "total_scanned": 0,
            "total_analyzed": 0,
            "enter_count": 0,
            "almost_count": 0,
            "wait_daily_count": 0,
            "watch_count": 0,
            "earnings_blocked": 0,
            "errors": 0,
            "positions": [],
        }


def positions_payload():
    """Open trades plus the exit verdict from the last scan. A trade recorded
    after that scan simply has no verdict yet - the UI shows NOT CHECKED and
    the 'Check now' button re-runs it live."""
    checked = {p.get("id"): p for p in (load_results().get("positions") or [])}
    rows = []
    for p in positions_store.open_positions():
        rows.append({**p, **checked.get(p.get("id"), {"action": "NOT CHECKED", "reasons": []})})
    return rows


@app.route("/")
def index():
    data = load_results()
    return render_template("index.html", data=data, positions=positions_payload())


@app.route("/api/results")
def api_results():
    return jsonify(load_results())


@app.route("/api/positions")
def api_positions():
    return jsonify({"status": "ok", "positions": positions_payload()})


@app.route("/api/positions", methods=["POST"])
def api_positions_add():
    body = request.get_json(silent=True) or {}
    try:
        positions_store.add_position(
            body.get("ticker"), body.get("entry_price"), body.get("shares"),
            body.get("stop"), body.get("entry_date"), body.get("note", ""))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify({"status": "ok", "positions": positions_payload()})


@app.route("/api/positions/<int:pos_id>/close", methods=["POST"])
def api_positions_close(pos_id):
    body = request.get_json(silent=True) or {}
    try:
        positions_store.close_position(pos_id, body.get("exit_price"))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify({"status": "ok", "positions": positions_payload()})


@app.route("/api/positions/check", methods=["POST"])
def api_positions_check():
    """Re-run the exit rules right now against fresh data. Does NOT touch
    latest.json - that stays whatever the scan wrote."""
    import scanner   # imported lazily: pulls yfinance/pandas
    try:
        return jsonify({"status": "ok", "positions": scanner.check_open_positions()})
    except Exception as e:
        return jsonify({"status": "error", "message": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/scan", methods=["POST"])
def trigger_scan():
    scanner = os.path.join(BASE, "scanner.py")
    subprocess.Popen(
        [sys.executable, scanner],
        cwd=BASE,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return jsonify({"status": "started", "message": "Scan started in new window"})


if __name__ == "__main__":
    print(f"Dashboard: http://localhost:{DASHBOARD_PORT}")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
