# -*- coding: utf-8 -*-
"""
scrape_and_outreach.py — Global Lead Outreach Engine
=====================================================
Scrapes dermatologist & dental clinics globally via Google Maps,
audits websites for modernity, sends outreach emails via SMTP,
and tracks replies via IMAP. Persists all data in SQLite (leads.db).

Usage:
    python scrape_and_outreach.py [--dry-run] [--limit N]
                                  [--keyword TERM] [--city CITY]
                                  [--no-scrape] [--no-email]
"""

import os
import sys
import time
import re
import json
import random
import sqlite3
import asyncio
import argparse
import requests
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import imaplib
import email as email_parser

# ---------------------------------------------------------------------------
# Path Setup — add parent dir so 'scraper' package is importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from scraper import GoogleMapsScraper, EmailFinder  # noqa: E402

# Default paths
DB_PATH     = os.path.join(SCRIPT_DIR, "leads.db")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
ENV_PATH    = os.path.join(SCRIPT_DIR, ".env")

# Auto-load .env if present
if os.path.exists(ENV_PATH):
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()
    except Exception as e:
        print(f"[Init] Warning: Could not load .env: {e}")

# Noise strings returned by Google Maps sidebar UI — not real businesses
_NOISE_NAMES = frozenset(["", "results", "more results", "advertisement", "sponsored"])

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> None:
    """Create the leads table and add a name index if they don't exist yet."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                name                  TEXT UNIQUE,
                phone                 TEXT,
                address               TEXT,
                website               TEXT,
                status                TEXT,
                email                 TEXT,
                email_status          TEXT DEFAULT 'Not Sent',
                sent_at               TEXT,
                followup_sent_at      TEXT,
                replied_at            TEXT,
                query                 TEXT,
                location              TEXT,
                scraped_at            TEXT,
                website_viewport      INTEGER,
                website_ssl           INTEGER,
                website_copyright_year INTEGER,
                website_notes         TEXT,
                email_error           TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_name ON leads (name)"
        )
        # Migration: add columns to pre-existing databases that lack them
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()
        }
        new_cols = [
            ("email_error", "TEXT"),
            ("reply_subject", "TEXT"),
            ("reply_body", "TEXT"),
            ("followup_sent_at", "TEXT"),
            ("agency_niche", "TEXT"),
            ("ats_system", "TEXT"),
            ("recruiter_count", "INTEGER DEFAULT 0"),
            ("active_open_roles", "INTEGER DEFAULT 0"),
            ("candidate_portal_type", "TEXT"),
            ("ai_screening_detected", "INTEGER DEFAULT 0"),
            ("technologies_used", "TEXT"),
            ("pain_points", "TEXT"),
            ("ai_opportunity_score", "INTEGER DEFAULT 0"),
            ("personalization_data", "TEXT"),
            ("reply_category", "TEXT"),
            ("contact_name", "TEXT"),
            ("job_title", "TEXT")
        ]
        for col_name, col_type in new_cols:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_type}")
        conn.commit()



# ---------------------------------------------------------------------------
# Website Modernity Analysis
# ---------------------------------------------------------------------------

_VIEWPORT_RE  = re.compile(r'<meta\s+name=["\']viewport["\']', re.IGNORECASE)
_COPYRIGHT_RE = re.compile(
    r'(?:copyright|\u00a9|&copy;)\s*(?:20[0-2]\d\s*-\s*)?(20[0-2]\d)',
    re.IGNORECASE
)
_COPYRIGHT_FB = re.compile(
    r'copyright.*?\b(20[0-2]\d)\b',
    re.IGNORECASE | re.DOTALL
)

# --- Booking system detection patterns ---
_BOOKING_PATTERNS = [
    r'calendly\.com',
    r'acuityscheduling\.com',
    r'booksy\.com',
    r'zocdoc\.com',
    r'doctolib',
    r'practo\.com',
    r'appointy\.com',
    r'simplybook\.me',
    r'square\.site.*book',
    r'setmore\.com',
    r'fresha\.com',
    r'vagaro\.com',
    r'mindbodyonline\.com',
    r'10to8\.com',
    r'picktime\.com',
    r'class="book',        # generic booking button/widget class
    r'id="book',
    r'book.?now',
    r'book.?appointment',
    r'schedule.?appointment',
    r'online.?booking',
    r'request.?appointment',
    r'type="submit"[^>]*book',
    r'<button[^>]*>\s*book',
]
_BOOKING_RE = re.compile('|'.join(_BOOKING_PATTERNS), re.IGNORECASE)

# --- AI / chatbot detection patterns ---
_AI_PATTERNS = [
    r'intercom\.io',
    r'intercomcdn\.com',
    r'drift\.com',
    r'driftt\.com',
    r'tidio\.com',
    r'crisp\.chat',
    r'freshchat',
    r'zendesk',
    r'hubspot.*chat',
    r'livechat',
    r'tawk\.to',
    r'chatbot',
    r'openai',
    r'gpt',
    r'dialogflow',
    r'botpress',
    r'manychat',
    r'ai.?assistant',
    r'ai.?chat',
    r'virtual.?assistant',
]
_AI_RE = re.compile('|'.join(_AI_PATTERNS), re.IGNORECASE)

# --- Contact form detection ---
_CONTACT_FORM_RE = re.compile(
    r'<form|contact.?form|enquiry.?form|patient.?form|intake.?form'
    r'|<input[^>]+type=["\'](?:email|tel)["\']'
    r'|mailto:',
    re.IGNORECASE
)

# --- Google Analytics / tracking detection ---
_ANALYTICS_RE = re.compile(
    r'google-analytics\.com|googletagmanager\.com|gtag\(|ga\(|fbq\(|_gaq'
    r'|hotjar|mixpanel|segment\.com|clarity\.ms',
    re.IGNORECASE
)

# --- Social media link detection ---
_SOCIAL_RE = re.compile(
    r'facebook\.com|instagram\.com|twitter\.com|x\.com|linkedin\.com'
    r'|youtube\.com|tiktok\.com|threads\.net',
    re.IGNORECASE
)

# --- Testimonial / review section detection ---
_REVIEW_RE = re.compile(
    r'testimonial|review|patient.said|what.our.patient|star.rating'
    r'|google.review|trustpilot|doctolib.*review|zocdoc.*review'
    r'|class=["\'][^"\'>]*(?:review|testimonial|rating)',
    re.IGNORECASE
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class SafeDict(dict):
    """Dictionary that returns default fallback strings for missing keys instead of raising KeyError."""
    def __missing__(self, key):
        defaults = {
            "contact_name": "Team",
            "ats_system": "your ATS",
            "agency_niche": "your recruitment niche",
            "active_open_roles": "open roles",
            "first_line_personalization": "",
            "company_personalization": "",
            "industry_personalization": "",
            "technology_personalization": "",
            "job_posting_personalization": "",
            "website_issues": "operational bottlenecks in candidate response time",
            "business_name": "your agency",
            "website_url": "your website",
            "city": "your city",
            "promo_url": "https://recruitment-ai-assistant.vercel.app/"
        }
        return defaults.get(key, f"{{{key}}}")


def safe_format(template_str: str, **kwargs) -> str:
    """Format template string safely without ever raising KeyError for missing placeholders."""
    if not template_str:
        return ""
    clean_kwargs = {k: (v if v is not None else "") for k, v in kwargs.items()}
    return template_str.format_map(SafeDict(**clean_kwargs))


def _build_fault_list(result: dict) -> list:
    """
    Convert analysis result dict into a list of specific, human-readable
    fault strings. Used to personalise outreach emails for recruitment agencies.
    """
    faults = []
    if not result["ssl"]:
        faults.append("no SSL certificate (website shows as 'Not Secure' in browsers)")
    if not result["viewport"]:
        faults.append("not mobile-friendly (broken layout on phones and tablets)")
    if result["copyright_year"] and result["copyright_year"] <= 2022:
        faults.append(f"outdated design (copyright shows {result['copyright_year']})")
    if not result["has_booking"]:
        faults.append("no instant candidate scheduling (candidates cannot book screening calls directly from the website)")
    if not result["has_ai"]:
        faults.append("no AI assistant or 24/7 candidate chat (no way to pre-screen applicants after business hours)")
    if not result["has_contact_form"]:
        faults.append("no candidate application or quick enquiry form")
    if not result["has_analytics"]:
        faults.append("no website analytics (no visibility into candidate drop-off or job board traffic)")
    if not result["has_social"]:
        faults.append("no social media or LinkedIn profile links visible on the website")
    if not result["has_reviews"]:
        faults.append("no client testimonials or candidate placement reviews section")
    return faults


def analyze_website(url: str) -> dict:
    """
    Fetch *url* and perform a comprehensive audit for 10 specific faults.

    Status hierarchy:
      'No Website'    - no URL present
      'Old Website'   - HTTP-only, no viewport, or copyright <= 2022
      'No Booking/AI' - modern site but lacks booking AND AI chatbot
      'Modern Website'- modern site WITH booking system or AI chat

    Returns dict with keys:
        ssl, viewport, copyright_year,
        has_booking, has_ai, has_contact_form,
        has_analytics, has_social, has_reviews,
        faults (list), status, notes
    """
    result = {
        "ssl": 0,
        "viewport": 0,
        "copyright_year": None,
        "has_booking": 0,
        "has_ai": 0,
        "has_contact_form": 0,
        "has_analytics": 0,
        "has_social": 0,
        "has_reviews": 0,
        "faults": [],
        "status": "Old Website",
        "notes": "",
    }

    if not url or not url.strip():
        result["status"] = "No Website"
        result["notes"]  = "No website listed."
        result["faults"] = ["no website at all"]
        return result

    fetch_url = url.strip()
    if not fetch_url.startswith(("http://", "https://")):
        fetch_url = "https://" + fetch_url

    try:
        resp = requests.get(
            fetch_url, timeout=5, headers=_HEADERS, allow_redirects=True
        )

        # 1. SSL
        if resp.url.startswith("https://"):
            result["ssl"] = 1

        if not resp.ok:
            result["notes"] += f"Server returned HTTP {resp.status_code}. "
            result["faults"] = _build_fault_list(result)
            return result

        html = resp.text

        # 2. Mobile viewport
        if _VIEWPORT_RE.search(html):
            result["viewport"] = 1

        # 3. Copyright year
        m = _COPYRIGHT_RE.search(html) or _COPYRIGHT_FB.search(html[:100_000])
        if m:
            year = int(m.group(1))
            result["copyright_year"] = year

        # 4. Booking system
        if _BOOKING_RE.search(html):
            result["has_booking"] = 1

        # 5. AI / chatbot
        if _AI_RE.search(html):
            result["has_ai"] = 1

        # 6. Contact form
        if _CONTACT_FORM_RE.search(html):
            result["has_contact_form"] = 1

        # 7. Analytics / tracking
        if _ANALYTICS_RE.search(html):
            result["has_analytics"] = 1

        # 8. Social media links
        if _SOCIAL_RE.search(html):
            result["has_social"] = 1

        # 9. Testimonials / reviews
        if _REVIEW_RE.search(html):
            result["has_reviews"] = 1

        # -- Status decision --
        is_old = (
            result["ssl"] == 0
            or result["viewport"] == 0
            or (result["copyright_year"] and result["copyright_year"] <= 2022)
        )
        if is_old:
            result["status"] = "Old Website"
        elif not result["has_booking"] and not result["has_ai"]:
            result["status"] = "No Booking/AI"
        else:
            result["status"] = "Modern Website"

        # Build fault list AFTER all checks are done
        result["faults"] = _build_fault_list(result)

        # Human-readable notes summary
        if result["faults"]:
            result["notes"] = f"Issues found: {'; '.join(result['faults'][:3])}."
        else:
            result["notes"] = "Website appears fully optimised."

    except requests.exceptions.Timeout:
        result["notes"]  = "Website timed out (very slow or unresponsive)."
        result["status"] = "Old Website"
        result["faults"] = ["website is very slow or unresponsive"] + _build_fault_list(result)
    except requests.exceptions.ConnectionError:
        result["notes"]  = "Could not connect to website."
        result["status"] = "Old Website"
        result["faults"] = ["website appears to be down or unreachable"] + _build_fault_list(result)
    except Exception as exc:
        result["notes"]  = f"Analysis error: {exc}."
        result["status"] = "Old Website"
        result["faults"] = _build_fault_list(result)

    return result

# ---------------------------------------------------------------------------
# Scraping Process
# ---------------------------------------------------------------------------

async def scrape_new_leads(
    db_path: str,
    config: dict,
    keyword_override: str | None = None,
    city_override:   str | None = None,
    limit: int = 20,
    immediate_outreach: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, bool]:
    """Run one Google Maps search, close scraper, then audit websites, persist results, and optionally send outreach immediately."""

    # Resolve keyword + niche
    if keyword_override:
        kw_obj = next(
            (k for k in config["keywords"] if k["term"].lower() == keyword_override.lower()),
            {"term": keyword_override, "niche": "dental"},   # safe fallback
        )
    else:
        kw_obj = random.choice(config["keywords"])

    target_kw: str = kw_obj["term"]
    niche:     str = kw_obj["niche"]
    city:      str = city_override or random.choice(config["cities"])

    print(f"\n[Scraper] Searching '{target_kw}' in '{city}' (niche: {niche}) — limit {limit}")

    init_db(db_path)
    email_finder = EmailFinder(delay=0.5, check_contact_pages=True)

    records = []
    # Collect all records from Maps scraper first and close browser context to avoid idle browser timeouts
    async with GoogleMapsScraper(headless=True, delay_ms=1800) as scraper:
        async for record in scraper.search(target_kw, city, max_results=limit):
            records.append(record)

    real_count = 0
    new_count  = 0
    emails_sent = 0
    cap_reached = False

    DAILY_CAP    = config.get("daily_email_limit", 50)
    SEND_GAP_SEC = config.get("send_gap_seconds", 60)

    # Process collected records
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        for idx, record in enumerate(records, 1):
            name = record.name.strip()

            # Filter noise
            if name.lower() in _NOISE_NAMES:
                continue

            real_count += 1

            # Duplicate check (fast via idx_leads_name index)
            cursor.execute(
                "SELECT id FROM leads WHERE name = ?", (name,)
            )
            if cursor.fetchone():
                print(f"  [Skip] Duplicate: '{name}'")
                continue

            website = (record.website or "").strip()
            print(f"\n  [{real_count}] '{name}'  |  {website or 'no website'}  |  {record.phone or 'no phone'}")

            # Audit website
            analysis = analyze_website(website)
            print(f"      -> {analysis['status']}  {analysis['notes'].strip()}")

            # Discover email only for Old Website/No Booking leads
            email = ""
            lead_status = analysis["status"]
            if website and lead_status in ["Old Website", "No Booking/AI"]:
                if "Could not connect" in analysis["notes"] or "timed out" in analysis["notes"]:
                    print("      -> Website down or timed out during analysis. Skipping email finder.")
                    email = ""
                else:
                    print("      -> Searching for contact email...")
                    email = email_finder.find(website)
                print(f"      -> Email: {email or 'not found'}")
                if not email:
                    lead_status = "Filtered (No Email)"
                    print("      -> Skipped from outreach queue (no email found)")

            # Check if we should immediately do outreach
            should_email = (
                immediate_outreach
                and lead_status in ["Old Website", "No Booking/AI"]
                and bool(email)
            )

            if should_email and email:
                cursor.execute(
                    "SELECT id, email_status FROM leads WHERE email = ? AND email_status NOT IN ('Not Sent', 'Filtered (No Email)')",
                    (email,)
                )
                existing_email = cursor.fetchone()
                if existing_email:
                    print(f"      -> [Skip] Duplicate Email: '{email}' already contacted ({existing_email[1]}).")
                    should_email = False

            if should_email:
                today_date = datetime.now().strftime("%Y-%m-%d")
                NEW_OUTREACH_CAP = config.get("new_outreach_min_per_day", 50)
                new_sent_today = conn.execute(
                    "SELECT COUNT(*) FROM leads WHERE sent_at LIKE ? AND email_status IN ('Sent', 'Sent (Dry Run)') AND followup_sent_at IS NULL",
                    (f"{today_date}%",)
                ).fetchone()[0]

                if new_sent_today >= NEW_OUTREACH_CAP:
                    print(f"      -> [Cap] Daily new outreach limit of {NEW_OUTREACH_CAP} reached today. Skipping immediate outreach.")
                    should_email = False

            # Persist lead to DB
            lead_id = None
            try:
                cursor.execute("""
                    INSERT INTO leads (
                        name, phone, address, website, status, email,
                        email_status, query, location, scraped_at,
                        website_viewport, website_ssl,
                        website_copyright_year, website_notes
                    ) VALUES (?,?,?,?,?,?,'Not Sent',?,?,?,?,?,?,?)
                """, (
                    name, record.phone, record.address,
                    website, lead_status, email,
                    target_kw, city, datetime.now().isoformat(),
                    analysis["viewport"], analysis["ssl"],
                    analysis["copyright_year"],
                    analysis["notes"].strip(),
                ))
                conn.commit()
                lead_id = cursor.lastrowid
                new_count += 1
            except sqlite3.IntegrityError:
                print(f"      → Integrity conflict skipped for '{name}'")
                continue
            except sqlite3.Error as db_err:
                print(f"      → DB error: {db_err}")
                continue

            # Send email immediately if eligible
            if should_email and lead_id and not cap_reached:
                success = send_single_email(
                    db_path=db_path,
                    lead_id=lead_id,
                    name=name,
                    email=email,
                    website=website,
                    query_term=target_kw,
                    lead_status=lead_status,
                    website_notes=analysis["notes"].strip(),
                    config=config,
                    dry_run=dry_run,
                )
                if success:
                    emails_sent += 1
                    # Check if cap reached now
                    today_date = datetime.now().strftime("%Y-%m-%d")
                    sent_today = conn.execute(
                        "SELECT COUNT(*) FROM leads WHERE sent_at LIKE ?",
                        (f"{today_date}%",)
                    ).fetchone()[0]

                    if sent_today >= DAILY_CAP:
                        print(f"      -> [Cap] Daily limit of {DAILY_CAP} reached after send. Stopping.")
                        cap_reached = True
                        break

                    # Wait before moving to next listing (unless dry-run or last record in results)
                    is_last_item = (idx == len(records))
                    if not is_last_item and not dry_run:
                        print(f"      -> [Wait] Pausing 10 minutes before next listing...")
                        time.sleep(SEND_GAP_SEC)
    finally:
        conn.close()

    print(f"\n[Scraper] Done. {real_count} real listings checked, {new_count} new leads added.")
    return new_count, emails_sent, cap_reached


# ---------------------------------------------------------------------------
# Target-Based Scraping Loop
# ---------------------------------------------------------------------------

async def scrape_until_target(
    db_path:     str,
    config:      dict,
    target:      int  = 400,
    limit:       int  = 150,
    max_iters:   int  = 100,
) -> None:
    """
    Repeatedly call scrape_new_leads with random city + keyword combinations
    until `target` Old Website leads have been collected today, or until
    `max_iters` search iterations are exhausted (safety cap).

    Each iteration picks a fresh (city, keyword) pair to maximise coverage
    and avoid repeating the same Google Maps page.
    """
    from datetime import date
    today = date.today().isoformat()

    def _count_today() -> int:
        """Count Old Website/No Booking leads WITH EMAILS scraped today."""
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM leads "
                "WHERE status IN ('Old Website', 'No Booking/AI') "
                "  AND email IS NOT NULL AND email != '' "
                "  AND scraped_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]

    for iteration in range(1, max_iters + 1):
        current = _count_today()
        print(f"\n{'='*60}")
        print(f"[Target] Iteration {iteration}/{max_iters}  |  Old Website leads today: {current}/{target}")
        print(f"{'='*60}")

        if current >= target:
            print(f"[Target] Goal reached! {current} outreach targets with email collected today. Done.")
            break

        remaining = target - current
        print(f"[Target] Need {remaining} more outreach targets with email — starting search...")

        try:
            await scrape_new_leads(db_path, config, limit=limit)
            git_sync(db_path)
        except Exception as err:
            print(f"[Target] Search iteration {iteration} failed: {err}")

    else:
        final = _count_today()
        print(f"\n[Target] Max iterations reached. Collected {final}/{target} Old Website leads today.")

    final = _count_today()
    print(f"\n[Target] Session complete. Total Old Website leads today: {final}")

# ---------------------------------------------------------------------------
# Follow-Up Engine (Global)
# ---------------------------------------------------------------------------

def send_followup_emails_global(
    db_path: str,
    config:  dict,
    budget_used: int = 0,
    dry_run: bool = False,
) -> int:
    """
    Send follow-up emails (global system) to leads that:
      - email_status = 'Sent'
      - sent_at >= follow_up_days (5) days ago
      - followup_sent_at IS NULL

    Capped at followup_max_per_day (20). Returns number sent.
    Excess follow-ups carry over to the next day — never dropped.
    """
    DAILY_CAP      = config.get("daily_email_limit", 100)
    FOLLOWUP_MAX   = config.get("followup_max_per_day", 50)
    FOLLOW_UP_DAYS = config.get("follow_up_days", 3)
    SEND_GAP_SEC   = config.get("send_gap_seconds", 60)

    remaining_budget = DAILY_CAP - budget_used
    followup_cap     = min(FOLLOWUP_MAX, remaining_budget)

    if followup_cap <= 0:
        print("[Follow-Up Global] No budget remaining for follow-ups today.")
        return 0

    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=FOLLOW_UP_DAYS)).strftime("%Y-%m-%d")

    with sqlite3.connect(db_path) as conn:
        due_leads = conn.execute("""
            SELECT id, name, email, website, query, location, status, website_notes
            FROM   leads
            WHERE  email_status    = 'Sent'
              AND  sent_at         < ?
              AND  followup_sent_at IS NULL
              AND  email           IS NOT NULL
              AND  email           != ''
            ORDER BY sent_at ASC
            LIMIT ?
        """, (cutoff + "T00:00:00", followup_cap)).fetchall()

    if not due_leads:
        print("[Follow-Up Global] No follow-ups due today.")
        return 0

    print(f"\n[Follow-Up Global] {len(due_leads)} follow-up(s) due (cap: {followup_cap}). Sending...")

    smtp_host     = os.environ.get("SMTP_HOST",     "smtp.gmail.com").strip()
    smtp_port     = int(os.environ.get("SMTP_PORT",  "587"))
    smtp_user     = os.environ.get("SMTP_USER",     "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    smtp_from     = os.environ.get("SMTP_FROM",     smtp_user).strip()

    sent_count = 0
    for lead in due_leads:
        lead_id, name, email, website, query_term, city, lead_status, website_notes = lead

        # Build follow-up body
        raw = website_notes or ""
        if "Issues found:" in raw:
            items = [i.strip() for i in raw.replace("Issues found:","").strip().rstrip(".").split(";") if i.strip()]
        else:
            items = [raw] if raw else ["several areas that could be improved"]
        issues = "\n".join(f"  {i+1}. {item.capitalize()}" for i, item in enumerate(items[:5]))

        # Load followup template from config if available, fallback to hardcoded
        templates = config.get("email_templates", {})
        template = templates.get("followup")

        niche = "general"
        for kw in config.get("keywords", []):
            if kw["term"].lower() in (query_term or "").lower():
                niche = kw["niche"]
                break
        promo_url = config.get("promo_urls", {}).get(niche, config.get("promo_urls", {}).get("general", ""))

        if template:
            subject = safe_format(
                template.get("subject", "Re: Quick AI candidate screening audit for {business_name}"),
                business_name=name,
                city=city or "your city",
                website_url=website or "your website",
                promo_url=promo_url,
                website_issues=issues,
            )
            body = safe_format(
                template.get("body", ""),
                business_name=name,
                website_url=website or "your website",
                promo_url=promo_url,
                website_issues=issues,
                city=city or "your city",
            )
        else:
            subject = f"Re: Quick website audit for {name}"
            body    = (
                f"Hi there,\n\n"
                f"I reached out a few days ago about {name}'s website ({website or 'your website'}) — "
                f"just following up in case my email got buried.\n\n"
                f"The main issues I flagged were:\n\n{issues}\n\n"
                f"Clinics that fix these typically see more online enquiries within the first month. "
                f"Happy to show you a quick 2-minute demo with no commitment.\n\n"
                f"Best regards,\nAditya Tyagi\nAI & Automation Engineer"
            )

        if dry_run:
            print(f"  [FOLLOW-UP DRY-RUN] -> {email}  |  {name}")
            print(f"  Subject: {subject}")
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "UPDATE leads SET email_status='Follow-Up Sent', followup_sent_at=? WHERE id=?",
                        (datetime.now().isoformat(), lead_id),
                    )
                    conn.commit()
            except sqlite3.Error:
                pass
            sent_count += 1
            continue

        if not (smtp_user and smtp_password):
            print(f"  [Follow-Up Global] SMTP credentials missing — skipping {email}.")
            continue

        try:
            msg = MIMEMultipart()
            msg["From"]    = smtp_from
            msg["To"]      = email
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.sendmail(smtp_from, [email], msg.as_string())

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE leads SET email_status='Follow-Up Sent', followup_sent_at=? WHERE id=?",
                    (datetime.now().isoformat(), lead_id),
                )
                conn.commit()

            print(f"  [Follow-Up Global][Sent] {email}  |  {name}")
            sent_count += 1
            git_sync(db_path)

            if sent_count < len(due_leads) and not dry_run:
                time.sleep(SEND_GAP_SEC)

        except Exception as err:
            print(f"  [Follow-Up Global][Error] {email}: {err}")
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "UPDATE leads SET email_error=? WHERE id=?",
                        (str(err), lead_id),
                    )
                    conn.commit()
            except sqlite3.Error:
                pass

    print(f"[Follow-Up Global] Done. {sent_count} follow-up(s) sent.")
    return sent_count


# ---------------------------------------------------------------------------
# Outbound Email Campaigns
# ---------------------------------------------------------------------------

DEFAULT_SMTP_USER = "aditya.airecruitment@gmail.com"
DEFAULT_SMTP_PASS = "psxirlbzfcixnfyl"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587

def _send_smtp_email(recipient: str, subject: str, body: str, config_user: str = "", config_pass: str = "") -> tuple[bool, str]:
    """
    Robust SMTP email sender with automatic failover.
    Tries provided/env credentials first; falls back to verified backup credentials on auth failure.
    """
    host = os.environ.get("SMTP_HOST", DEFAULT_SMTP_HOST).strip()
    try:
        port = int(os.environ.get("SMTP_PORT", DEFAULT_SMTP_PORT))
    except Exception:
        port = DEFAULT_SMTP_PORT

    env_user = os.environ.get("SMTP_USER", "").strip() or config_user.strip()
    env_pass = os.environ.get("SMTP_PASSWORD", "").strip() or config_pass.strip()
    from_addr = os.environ.get("SMTP_FROM", "").strip() or env_user or DEFAULT_SMTP_USER

    creds_to_try = []
    if env_user and env_pass:
        creds_to_try.append((env_user, env_pass, from_addr))
    if (DEFAULT_SMTP_USER, DEFAULT_SMTP_PASS, DEFAULT_SMTP_USER) not in creds_to_try:
        creds_to_try.append((DEFAULT_SMTP_USER, DEFAULT_SMTP_PASS, DEFAULT_SMTP_USER))

    last_err = None
    for u, p, sender in creds_to_try:
        try:
            msg = MIMEMultipart()
            msg["From"] = sender
            msg["To"] = recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(host, port, timeout=25) as server:
                server.ehlo()
                server.starttls()
                server.login(u, p)
                server.sendmail(sender, [recipient], msg.as_string())
            return True, u
        except Exception as err:
            last_err = err
            print(f"  [SMTP Auth Warning] Could not send via {u}: {err}. Retrying fallback...")

    if last_err:
        raise last_err
    raise RuntimeError("No SMTP credentials available.")


def send_single_email(
    db_path: str,
    lead_id: int,
    name: str,
    email: str,
    website: str,
    query_term: str,
    lead_status: str,
    website_notes: str,
    config: dict,
    dry_run: bool = False,
) -> bool:
    """
    Send outreach email to a single lead.
    Returns True if sent successfully (or dry-run), False otherwise.
    """
    is_dry_run = dry_run

    # Resolve niche from keyword
    niche = "dental"
    for kw in config.get("keywords", []):
        if kw["term"].lower() in (query_term or "").lower():
            niche = kw["niche"]
            break

    # Fetch city/location from database for this lead
    city = ""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT location FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if row and row[0]:
                city = row[0].strip()
    except Exception:
        pass
    if not city:
        city = "your city"

    # Format the specific website issues as a numbered list for the email
    raw_notes = website_notes or ""
    if "Issues found:" in raw_notes:
        issues_raw = raw_notes.replace("Issues found:", "").strip().rstrip(".")
        issue_items = [i.strip() for i in issues_raw.split(";") if i.strip()]
    else:
        issue_items = [raw_notes] if raw_notes else ["several areas that could be improved"]
    website_issues = "\n".join(
        f"  {i+1}. {item.capitalize()}" for i, item in enumerate(issue_items[:5])
    )

    # Choose template based on lead type
    if lead_status == "No Booking/AI":
        template_key = f"{niche}_no_booking_ai"
        template = (
            config.get("email_templates", {}).get(template_key)
            or config.get("email_templates", {}).get("no_booking_ai")
            or config.get("email_templates", {}).get(niche)
            or config.get("email_templates", {}).get("general_staffing")
            or (list(config.get("email_templates", {}).values()) or [None])[0]
        )
    else:
        template = (
            config.get("email_templates", {}).get(niche)
            or config.get("email_templates", {}).get("general_staffing")
            or (list(config.get("email_templates", {}).values()) or [None])[0]
        )

    if not template:
        print(f"  [Outreach] No email template found for niche '{niche}' — skipping {email}.")
        return False

    promo_url = config.get("promo_urls", {}).get(niche, config.get("promo_urls", {}).get("general_staffing", ""))
    subject = safe_format(
        template.get("subject", "Quick AI candidate screening audit for {business_name}"),
        business_name=name,
        city=city or "your city",
        website_url=website or "your website",
        promo_url=promo_url,
        website_issues=website_issues,
    )
    body = safe_format(
        template.get("body", ""),
        business_name=name,
        website_url=website or "your website",
        promo_url=promo_url,
        website_issues=website_issues,
        city=city or "your city",
    )

    if is_dry_run:
        print(f"\n  [DRY-RUN][{lead_status}] -> {email}  |  {name}")
        print(f"  Subject : {subject}")
        print(f"  Preview : {body[:180]}...")
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE leads SET email_status='Sent (Dry Run)', sent_at=? WHERE id=?",
                    (datetime.now().isoformat(), lead_id),
                )
                conn.commit()
            git_sync(db_path)
            return True
        except sqlite3.Error as e:
            print(f"  [Error] DB update failed: {e}")
            return False

    # Real send with failover
    try:
        ok, used_account = _send_smtp_email(
            recipient=email,
            subject=subject,
            body=body,
            config_user=config.get("smtp_user", ""),
            config_pass=config.get("smtp_password", "")
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE leads SET email_status='Sent', sent_at=? WHERE id=?",
                (datetime.now().isoformat(), lead_id),
            )
            conn.commit()
        print(f"  [Sent via {used_account}][{lead_status}] {email}  |  {name}")
        git_sync(db_path)
        return True

    except Exception as mail_err:
        print(f"  [Error] {email}: {mail_err}")
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE leads SET email_status='Failed', email_error=? WHERE id=?",
                    (str(mail_err), lead_id),
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"  [Error] DB update failed: {e}")
        git_sync(db_path)
        return False


def send_outreach_emails(
    db_path: str,
    config:  dict,
    budget_used: int = 0,
    dry_run: bool = False,
) -> int:
    """
    Send outreach emails to unsent leads in the database up to the daily cap.
    Returns the number of emails successfully sent (or dry-run).
    """
    is_dry_run = dry_run

    DAILY_CAP    = config.get("daily_email_limit", 50)
    MIN_NEW_LEADS = 10   # Only start emailing when >= 10 new leads exist today
    SEND_GAP_SEC = config.get("send_gap_seconds", 60)
    today         = datetime.now().strftime("%Y-%m-%d")

    with sqlite3.connect(db_path) as conn:
        # How many new leads scraped today? (freshness gate)
        new_today = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE scraped_at LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]

        print(f"\n[Outreach] Leads scraped today: {new_today}  |  Total campaign budget used: {budget_used}/{DAILY_CAP}")

        # Check if freshness gate is bypassed (e.g. by setting MIN_NEW_LEADS to 0 in memory)
        bypass_freshness = (globals().get("MIN_NEW_LEADS", 10) == 0)
        if new_today < MIN_NEW_LEADS and not bypass_freshness:
            print(f"[Outreach] Freshness gate: only {new_today} new leads today "
                  f"(need {MIN_NEW_LEADS}). Skipping email stage.")
            return 0

        if budget_used >= DAILY_CAP:
            print(f"[Outreach] Daily cap of {DAILY_CAP} emails already reached. Done.")
            return 0

        remaining_slots = DAILY_CAP - budget_used

        # Pull eligible leads: Old Website + No Booking/AI — unsent, have email, deduplicated by email
        leads_to_email = conn.execute("""
            SELECT id, name, email, website, query, location, status, website_notes
            FROM   leads
            WHERE  status       IN ('Old Website', 'No Booking/AI')
              AND  email_status  = 'Not Sent'
              AND  email         IS NOT NULL
              AND  email         != ''
              AND  email NOT IN (
                  SELECT email FROM leads WHERE email_status IN ('Sent', 'Sent (Dry Run)', 'Follow-Up Sent', 'Replied', 'Bounced')
              )
            GROUP BY email
            ORDER BY scraped_at DESC
            LIMIT ?
        """, (remaining_slots,)).fetchall()

    if not leads_to_email:
        print("[Outreach] No eligible leads in queue.")
        return 0

    print(f"[Outreach] {len(leads_to_email)} leads to contact (cap remaining: {remaining_slots}).")
    if is_dry_run:
        print("  [DRY-RUN] Emails will be logged, not dispatched (no 10-min wait).")

    sent_count = 0
    for lead in leads_to_email:
        lead_id, name, email, website, query_term, location, lead_status, website_notes = lead
        success = send_single_email(
            db_path=db_path,
            lead_id=lead_id,
            name=name,
            email=email,
            website=website,
            query_term=query_term,
            lead_status=lead_status,
            website_notes=website_notes,
            config=config,
            dry_run=is_dry_run,
        )
        if success:
            sent_count += 1
            if sent_count + budget_used >= DAILY_CAP:
                print(f"  [Cap] Daily limit of {DAILY_CAP} reached. Stopping.")
                break
            # Wait between sends
            remaining = len(leads_to_email) - sent_count
            if remaining > 0:
                if not is_dry_run:
                    print(f"  [Wait] Pausing {SEND_GAP_SEC}s before next email ({remaining} remaining)...")
                    time.sleep(SEND_GAP_SEC)

    print(f"[Outreach] Finished. {sent_count} emails dispatched today (total today: {sent_count + budget_used}).")
    return sent_count


# ---------------------------------------------------------------------------
# Real-time Git Sync
# ---------------------------------------------------------------------------

def git_sync(db_path: str):
    """Export data.json and commit/push changes in real-time during run."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    # Write sender email first
    try:
        smtp_from = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")).strip()
        if smtp_from:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            txt_path = os.path.join(script_dir, "sender_email.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(smtp_from)
    except Exception as e:
        print(f"  [Real-time Sync] Failed to write sender_email.txt: {e}")

    # Export data.json first
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import generate_data_json
        generate_data_json.main()
    except Exception as export_err:
        print(f"  [Real-time Sync] Failed to export data.json: {export_err}")
        return

    # Commit and push
    try:
        import subprocess
        # Configure user if needed
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True, capture_output=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True, capture_output=True)
        
        # Add files (relative paths from repo root)
        subprocess.run(["git", "add", "leads.db", "data.json", "sender_email.txt"], check=True, capture_output=True)
        
        # Check if changes exist
        diff_res = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff_res.returncode == 0:
            return # No changes
            
        # Commit
        subprocess.run([
            "git", "commit", "-m", "chore: real-time dashboard sync [skip ci]"
        ], check=True, capture_output=True)
        
        # Pull with rebase
        subprocess.run(["git", "pull", "--rebase"], check=True, capture_output=True)
        
        # Push
        subprocess.run(["git", "push"], check=True, capture_output=True)
        print("  [Real-time Sync] Database & dashboard updated on GitHub Pages.")
    except subprocess.CalledProcessError as git_err:
        stderr_msg = git_err.stderr.decode('utf-8', errors='ignore') if git_err.stderr else ''
        print(f"  [Real-time Sync] Git command failed: {stderr_msg}")
    except Exception as err:
        print(f"  [Real-time Sync] Unexpected error: {err}")


# ---------------------------------------------------------------------------
# Inbound Reply Tracking
# ---------------------------------------------------------------------------

def decode_mime_header(header_val: str) -> str:
    if not header_val:
        return ""
    try:
        from email.header import decode_header
        decoded = decode_header(header_val)
        parts = []
        for text, encoding in decoded:
            if isinstance(text, bytes):
                parts.append(text.decode(encoding or "utf-8", errors="ignore"))
            else:
                parts.append(str(text))
        return "".join(parts)
    except Exception:
        return header_val


def get_email_body_text(msg) -> str:
    body = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode('utf-8', errors='ignore')
                        break
                elif content_type == "text/html" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode('utf-8', errors='ignore')
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode('utf-8', errors='ignore')
    except Exception:
        pass
    
    # Strip HTML tags if any HTML tags are present
    if body and ("<html" in body.lower() or "<div" in body.lower() or "<p" in body.lower()):
        body = re.sub(r'<[^>]+>', '', body)
    
    if body:
        body = re.sub(r'\s+', ' ', body).strip()
    return body or ""


def check_for_replies(db_path: str) -> None:
    """Check IMAP inbox for replies from leads we pitched."""

    imap_user     = os.environ.get("IMAP_USER",     "").strip()
    imap_password = os.environ.get("IMAP_PASSWORD", "").strip()
    imap_host     = os.environ.get("IMAP_HOST",     "imap.gmail.com").strip()

    if not (imap_user and imap_password):
        print("\n[IMAP Tracker] Credentials missing — skipping reply check.")
        return

    # Load sent-lead emails into a lookup dict  {sender_email → lead_id}
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, email FROM leads
            WHERE email_status IN ('Sent', 'Sent (Dry Run)')
              AND email IS NOT NULL AND email != ''
        """)
        active_leads = {
            row[1].lower().strip(): row[0]
            for row in cursor.fetchall()
        }

    if not active_leads:
        print("\n[IMAP Tracker] No active leads awaiting replies.")
        return

    print(f"\n[IMAP Tracker] Connecting to {imap_host} …")
    replies_found = 0

    try:
        with imaplib.IMAP4_SSL(imap_host) as mail:
            mail.login(imap_user, imap_password)
            mail.select("INBOX")

            status, data = mail.search(None, "UNSEEN")
            if status != "OK" or not data[0]:
                status, data = mail.search(None, "ALL")

            if status != "OK" or not data[0]:
                print("  No messages found in inbox.")
                return

            email_ids = data[0].split()
            max_check = min(200, len(email_ids))
            print(f"  Scanning last {max_check} messages …")

            # FIX #6: correct slice — iterate from newest backwards, safely
            ids_to_scan = email_ids[-max_check:]   # last N items
            updated_ids = []

            for msg_id in reversed(ids_to_scan):
                res, msg_data = mail.fetch(msg_id, "(RFC822)")
                if res != "OK":
                    continue

                for part in msg_data:
                    if not isinstance(part, tuple):
                        continue
                    msg  = email_parser.message_from_bytes(part[1])
                    from_hdr = msg.get("From") or ""

                    # Robustly extract bare email addr from "Name <addr>" or plain addr
                    m = re.search(r'<([^>]+)>', from_hdr)
                    sender = (m.group(1) if m else from_hdr).lower().strip()

                    if sender in active_leads:
                        lead_id = active_leads[sender]
                        subject_hdr = decode_mime_header(msg.get("Subject") or "")
                        body_text = get_email_body_text(msg)
                        body_text = body_text[:1000] # limit to 1000 chars
                        
                        updated_ids.append((lead_id, sender, subject_hdr, body_text))
                        replies_found += 1

            # Batch-update database
            if updated_ids:
                with sqlite3.connect(db_path) as conn:
                    for lead_id, sender, subject_hdr, body_text in updated_ids:
                        conn.execute(
                            "UPDATE leads SET email_status='Replied', replied_at=?, reply_subject=?, reply_body=? WHERE id=?",
                            (datetime.now().isoformat(), subject_hdr, body_text, lead_id),
                        )
                        print(f"  [Reply] '{sender}' replied!")
                    conn.commit()

    except Exception as imap_err:
        print(f"  [IMAP Error] {imap_err}")

    if replies_found:
        print(f"  {replies_found} replies recorded.")
    else:
        print("  No new replies detected.")

# ---------------------------------------------------------------------------
async def run_continuous_outreach_loop(
    db_path: str,
    config: dict,
    limit: int = 25,
    max_iters: int = 20,
    dry_run: bool = False,
    keyword_override: str | None = None,
    city_override: str | None = None,
    target_override: int = 0,
) -> None:
    """
    Continuous loop that:
      1. First emails any eligible unsent leads in the database up to the daily cap.
      2. If the cap is not met, runs the scraper to extract new leads and immediately sends emails to targets.
      3. Repeats scraping across cities/keywords until the daily cap has been met or max_iters is reached.
    """
    DAILY_CAP = target_override if target_override > 0 else config.get("daily_email_limit", 50)
    today = datetime.now().strftime("%Y-%m-%d")

    def _count_sent_today() -> int:
        with sqlite3.connect(db_path) as c:
            return c.execute(
                "SELECT COUNT(*) FROM leads WHERE sent_at LIKE ? OR followup_sent_at LIKE ?",
                (f"{today}%", f"{today}%")
            ).fetchone()[0]

    sent_today = _count_sent_today()
    print(f"\n[Global Outreach] Campaign starting. Emails sent today: {sent_today}/{DAILY_CAP}")

    if sent_today >= DAILY_CAP:
        print(f"[Global Outreach] Daily cap already reached. Nothing to do.")
        return

    # ── Step 1: Follow-ups (max 20, fired FIRST) ──────────────────────
    print("\n[Global Outreach] Step 1 — Sending follow-up emails...")
    followups_sent = send_followup_emails_global(db_path, config, budget_used=sent_today, dry_run=dry_run)
    sent_today = _count_sent_today()
    git_sync(db_path)

    if sent_today >= DAILY_CAP:
        print("[Global Outreach] Daily cap reached after follow-ups.")
        return

    # ── Step 2: Email existing unsent leads from queue ─────────────────
    print("\n[Global Outreach] Step 2 — Emailing unsent leads from queue...")
    global MIN_NEW_LEADS
    old_min = globals().get("MIN_NEW_LEADS", 10)
    globals()["MIN_NEW_LEADS"] = 0
    try:
        send_outreach_emails(db_path, config, budget_used=sent_today, dry_run=dry_run)
    finally:
        globals()["MIN_NEW_LEADS"] = old_min

    sent_today = _count_sent_today()
    print(f"[Global Outreach] After queue, emails sent today: {sent_today}/{DAILY_CAP}")

    if sent_today >= DAILY_CAP:
        print("[Global Outreach] Daily cap reached via queue leads. Done.")
        return

    # Step 3: Loop scraping & immediate emailing until cap is met
    for iteration in range(1, max_iters + 1):
        with sqlite3.connect(db_path) as conn:
            sent_today = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE sent_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]

        if sent_today >= DAILY_CAP:
            print(f"[Outreach Loop] Goal reached! {sent_today}/{DAILY_CAP} emails sent today.")
            break

        remaining = DAILY_CAP - sent_today
        print(f"\n[Outreach Loop] Iteration {iteration}/{max_iters} — Need {remaining} more emails to hit daily cap.")

        # Run scraper with immediate outreach enabled
        try:
            new_leads, emails_sent, cap_reached = await scrape_new_leads(
                db_path=db_path,
                config=config,
                keyword_override=keyword_override,
                city_override=city_override,
                limit=limit,
                immediate_outreach=True,
                dry_run=dry_run,
            )
            print(f"[Outreach Loop] Iteration complete: added {new_leads} new leads, sent {emails_sent} outreach emails.")
            git_sync(db_path)
            if cap_reached:
                print("[Outreach Loop] Daily email cap reached during scraping. Done.")
                break
        except Exception as err:
            print(f"[Outreach Loop] Scraping iteration {iteration} failed: {err}")

    else:
        with sqlite3.connect(db_path) as conn:
            final_sent = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE sent_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]
        print(f"\n[Outreach Loop] Max iterations ({max_iters}) reached. Final daily count: {final_sent}/{DAILY_CAP}")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Global Outreach — Scraper + Email Campaign Engine"
    )
    parser.add_argument("--dry-run",      action="store_true",
                        help="Log emails to console instead of sending")
    parser.add_argument("--limit",        type=int, default=150,
                        help="Max Google Maps results per single search (default: 150)")
    parser.add_argument("--target",       type=int, default=400,
                        help="Keep scraping until this many qualified leads are saved today (default: 400)")
    parser.add_argument("--max-iterations", type=int, default=100,
                        help="Safety cap: max search loops to hit target (default: 100)")
    parser.add_argument("--keyword",      type=str, default=None,
                        help="Override keyword (e.g. 'dentist')")
    parser.add_argument("--city",         type=str, default=None,
                        help="Override city (e.g. 'London')")
    parser.add_argument("--no-scrape",    action="store_true",
                        help="Skip scraping; run email + reply stages only")
    parser.add_argument("--no-email",     action="store_true",
                        help="Skip email + reply stages; run scraper only")
    args = parser.parse_args()

    # Load config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except Exception as exc:
        print(f"[Error] Cannot load config.json: {exc}")
        sys.exit(1)

    init_db(DB_PATH)

    # Stage 1: Check replies (keeps state fresh)
    if not args.no_email:
        check_for_replies(DB_PATH)

    # Stage 2: Main workflow execution
    if args.no_scrape:
        # Email only mode
        if not args.no_email:
            followups = send_followup_emails_global(DB_PATH, config, dry_run=args.dry_run)
            send_outreach_emails(DB_PATH, config, budget_used=followups, dry_run=args.dry_run)
    elif args.no_email:
        # Scrape only mode
        try:
            asyncio.run(
                scrape_new_leads(
                    DB_PATH, config,
                    keyword_override=args.keyword,
                    city_override=args.city,
                    limit=args.limit,
                    immediate_outreach=False,
                    dry_run=args.dry_run,
                )
            )
        except Exception as err:
            print(f"[Fatal] Scraping failed: {err}")
    else:
        # Default: combined continuous outreach loop
        try:
            asyncio.run(
                run_continuous_outreach_loop(
                    DB_PATH, config,
                    limit=args.limit,
                    max_iters=args.max_iterations,
                    dry_run=args.dry_run,
                    keyword_override=args.keyword,
                    city_override=args.city,
                    target_override=args.target,
                )
            )
        except Exception as err:
            print(f"[Fatal] Continuous outreach loop failed: {err}")
