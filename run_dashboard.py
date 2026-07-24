# -*- coding: utf-8 -*-
"""
run_dashboard.py — Local Flask Server for Global Outreach Dashboard
====================================================================
Serves the dashboard UI at http://localhost:5002 and exposes API
endpoints that the frontend uses to read/write leads.db and config.json.

Start with:
    python global-outreach/run_dashboard.py
"""

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(SCRIPT_DIR, "leads.db")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    """Return a Row-factory–enabled connection to leads.db."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db() -> None:
    """Create the schema if leads.db does not yet exist.
    FIX #3: Import init_db correctly from the same directory.
    """
    if not os.path.exists(DB_PATH):
        # Add script dir to path so we can import from the same folder
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)
        from scrape_and_outreach import init_db   # same-dir import
        init_db(DB_PATH)

# ---------------------------------------------------------------------------
# Static file routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_file(os.path.join(SCRIPT_DIR, "dashboard.html"))


@app.route("/dashboard.css")
def css():
    return send_file(os.path.join(SCRIPT_DIR, "dashboard.css"), mimetype="text/css")


@app.route("/dashboard.js")
def js():
    return send_file(os.path.join(SCRIPT_DIR, "dashboard.js"), mimetype="application/javascript")

# ---------------------------------------------------------------------------
# API — Leads
# ---------------------------------------------------------------------------

@app.route("/api/leads", methods=["GET"])
def get_leads():
    ensure_db()
    status_filter = request.args.get("status")
    email_filter  = request.args.get("email_status")

    sql    = "SELECT * FROM leads"
    params = []
    where  = ["status != 'Filtered (No Email)'"]

    if status_filter:
        where.append("status = ?")
        params.append(status_filter)
    if email_filter:
        where.append("email_status = ?")
        params.append(email_filter)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY scraped_at DESC"

    with _get_conn() as conn:
        leads = [dict(row) for row in conn.execute(sql, params).fetchall()]

    return jsonify(leads)

# ---------------------------------------------------------------------------
# API — Stats
# FIX O1 / #12: single aggregate query replaces 7 separate round-trips
# ---------------------------------------------------------------------------

@app.route("/api/stats", methods=["GET"])
def get_stats():
    ensure_db()
    sql = """
        SELECT
            COUNT(*)                                              AS total,
            SUM(status = 'No Website')                           AS no_website,
            SUM(status IN ('Old Website', 'No Booking/AI') AND email IS NOT NULL AND email != '') AS old_website,
            SUM(status = 'Modern Website')                       AS modern_website,
            SUM(email_status IN ('Sent','Sent (Dry Run)', 'Replied')) AS sent,
            SUM(email_status = 'Replied')                        AS replied,
            SUM(email_status = 'Failed')                         AS failed
        FROM leads
        WHERE status != 'Filtered (No Email)'
    """
    with _get_conn() as conn:
        row = conn.execute(sql).fetchone()

    total, no_website, old_website, modern_website, sent, replied, failed = (
        row["total"]        or 0,
        row["no_website"]   or 0,
        row["old_website"]  or 0,
        row["modern_website"] or 0,
        row["sent"]         or 0,
        row["replied"]      or 0,
        row["failed"]       or 0,
    )

    # FIX #8: clamp conversion rate to [0, 100]
    conversion = round(min(replied / sent * 100, 100) if sent > 0 else 0, 1)

    return jsonify({
        "total":           total,
        "no_website":      no_website,
        "old_website":     old_website,
        "modern_website":  modern_website,
        "sent":            sent,
        "replied":         replied,
        "failed":          failed,
        "conversion_rate": conversion,
    })

# ---------------------------------------------------------------------------
# API — Config
# ---------------------------------------------------------------------------

@app.route("/api/config", methods=["GET", "POST"])
def config_endpoint():
    if request.method == "GET":
        if not os.path.exists(CONFIG_PATH):
            return jsonify({"error": "config.json not found"}), 404
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return jsonify(json.load(fh))

    # POST — save new config
    new_config = request.get_json(force=True)
    if new_config is None:
        return jsonify({"error": "Invalid JSON body"}), 400
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(new_config, fh, indent=2)
    return jsonify({"success": True, "message": "Configuration saved."})

# ---------------------------------------------------------------------------
# API — CSV Export (No Website leads)
# ---------------------------------------------------------------------------

@app.route("/api/export-no-website", methods=["GET"])
def export_no_website():
    ensure_db()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT name, phone, address, query, location, scraped_at
            FROM leads
            WHERE status = 'No Website'
            ORDER BY scraped_at DESC
        """).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Phone", "Address", "Search Keyword", "City", "Scraped At"])
    writer.writerows([tuple(r) for r in rows])
    buf.seek(0)

    return send_file(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="no_website_leads.csv",
    )

# ---------------------------------------------------------------------------
# API — Trigger Background Scrape
# ---------------------------------------------------------------------------

@app.route("/api/trigger-scrape", methods=["POST"])
def trigger_scrape():
    data    = request.get_json(force=True) or {}
    limit   = int(data.get("limit", 20))
    keyword = data.get("keyword") or None
    city    = data.get("city")    or None
    dry_run = bool(data.get("dry_run", True))

    def _run_bg():
        script = os.path.join(SCRIPT_DIR, "scrape_and_outreach.py")
        cmd = [sys.executable, script, f"--limit={limit}"]
        if keyword:
            cmd += ["--keyword", keyword]
        if city:
            cmd += ["--city", city]
        if dry_run:
            cmd.append("--dry-run")
        print(f"[Dashboard] Background job: {' '.join(cmd)}")
        subprocess.run(cmd, cwd=SCRIPT_DIR)
        print("[Dashboard] Background job finished.")

    threading.Thread(target=_run_bg, daemon=True).start()
    return jsonify({
        "success": True,
        "message": (
            "Scraper background task initiated. "
            "Refresh the dashboard in ~60 seconds to see new leads."
        ),
    })

# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ensure_db()
    port = 5002
    print("=" * 60)
    print("  GLOBAL OUTREACH DASHBOARD — Local Server")
    print(f"  Open http://localhost:{port} in your browser")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
