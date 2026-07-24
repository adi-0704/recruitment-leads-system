# -*- coding: utf-8 -*-
"""
generate_data_json.py
=====================
Reads leads.db and exports all leads + stats to data.json so the
dashboard can be served as a static GitHub Pages site without Flask.

Run automatically by GitHub Actions after every scrape/outreach run.
"""
import json
import os
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR, "leads.db")
OUT_PATH   = os.path.join(SCRIPT_DIR, "data.json")


def main():
    if not os.path.exists(DB_PATH):
        print("[data-export] leads.db not found — writing empty data.json")
        payload = {"leads": [], "stats": _empty_stats(), "generated_at": _now()}
        _write(payload)
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        leads = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM leads WHERE status != 'Filtered (No Email)' ORDER BY scraped_at DESC"
            ).fetchall()
        ]

        row = conn.execute("""
            SELECT
                COUNT(*)                                              AS total,
                SUM(status = 'No Website')                           AS no_website,
                SUM(status IN ('Old Website', 'No Booking/AI') AND email IS NOT NULL AND email != '') AS old_website,
                SUM(status = 'Modern Website')                       AS modern_website,
                SUM(email_status IN ('Sent','Sent (Dry Run)', 'Follow-Up Sent', 'Replied')) AS sent,
                SUM(email_status = 'Follow-Up Sent')                 AS followup_sent,
                SUM(email_status = 'Replied')                        AS replied,
                SUM(email_status = 'Failed')                         AS failed,
                SUM(ai_opportunity_score >= 70)                       AS high_ai_score,
                SUM(ats_system IS NOT NULL AND ats_system NOT IN ('', 'None Detected')) AS ats_detected,
                SUM(reply_category IN ('Meeting Requested', 'Interested')) AS meetings_booked
            FROM leads
            WHERE status != 'Filtered (No Email)'
        """).fetchone()

    stats = {k: (row[k] or 0) for k in row.keys()}
    sent    = stats["sent"]
    replied = stats["replied"]
    stats["conversion_rate"] = round(
        min(replied / sent * 100, 100) if sent > 0 else 0, 1
    )


    # Follow-ups due (≥5 days since sent, no followup yet)
    with sqlite3.connect(DB_PATH) as conn:
        stats["followups_due"] = conn.execute("""
            SELECT COUNT(*) FROM leads
            WHERE email_status = 'Sent'
              AND followup_sent_at IS NULL
              AND sent_at < datetime('now', '-5 days')
        """).fetchone()[0] or 0

    # Build a pre-filtered CSV string for no-website leads (used for download)
    no_website_leads = [l for l in leads if l.get("status") == "No Website"]
    csv_lines = ["Name,Phone,Address,Search Keyword,City,Scraped At"]
    for l in no_website_leads:
        def esc(v):
            v = str(v or "").replace('"', '""')
            return f'"{v}"'
        csv_lines.append(",".join([
            esc(l.get("name")), esc(l.get("phone")), esc(l.get("address")),
            esc(l.get("query")), esc(l.get("location")), esc(l.get("scraped_at"))
        ]))
    csv_content = "\n".join(csv_lines)

    sender_email = ""
    try:
        txt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sender_email.txt")
        if os.path.exists(txt_path):
            with open(txt_path, "r", encoding="utf-8") as f:
                sender_email = f.read().strip()
    except Exception:
        pass
        
    if not sender_email:
        sender_email = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")).strip()

    payload = {
        "leads":        leads,
        "stats":        stats,
        "no_website_csv": csv_content,
        "sender_email": sender_email,
        "generated_at": _now(),
    }
    _write(payload)
    print(f"[data-export] Exported {len(leads)} leads -> data.json  |  Stats: {stats}")


def _empty_stats():
    return {
        "total": 0, "no_website": 0, "old_website": 0,
        "modern_website": 0, "sent": 0, "replied": 0,
        "failed": 0, "conversion_rate": 0.0,
    }


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _write(payload):
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


if __name__ == "__main__":
    main()
