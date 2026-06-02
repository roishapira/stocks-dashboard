"""
DiCarlo BX Scanner - Web Dashboard
Run with: python dashboard.py
Open: http://localhost:5555
"""

from flask import Flask, render_template, jsonify
import json
import os
import subprocess
import sys

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
        }


@app.route("/")
def index():
    data = load_results()
    return render_template("index.html", data=data)


@app.route("/api/results")
def api_results():
    return jsonify(load_results())


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
