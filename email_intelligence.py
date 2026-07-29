"""
email_intelligence.py — AI-Powered Email Intelligence Engine
=============================================================
Central intelligence module for all 3 outreach workflows.

Features:
  ✓ MX record validation (skip invalid email domains before sending)
  ✓ Disposable email detection (skip throwaway addresses)
  ✓ Lead scoring 0–100 (prioritise best prospects)
  ✓ Smart send-time awareness (avoid weekends, bad hours)
  ✓ Subject-line A/B rotation (avoids spam filter pattern-matching)
  ✓ Daily HTML summary emails (know exactly what ran)
  ✓ Bounce-rate guard (pause if >5% bounce rate detected)
  ✓ Email warm-up scheduler (ramp from 5→25 safely over 14 days)

Usage (imported by all 3 outreach scripts):
    from email_intelligence import (
        validate_email, score_lead, get_best_send_window,
        rotate_subject, send_run_summary, check_bounce_guard
    )

Author: Aditya Tyagi <aditya.airecruitment@gmail.com>
Version: 2.0.0
"""

import os
import re
import sys
import time
import socket
import random
import sqlite3
import smtplib
import logging
from datetime import datetime, date, timedelta
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid

# ─── Logging ─────────────────────────────────────────────────────────────────
logger = logging.getLogger("email_intelligence")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s [EI] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ─── Constants ────────────────────────────────────────────────────────────────

# Domains known to be disposable / spam traps
_DISPOSABLE_DOMAINS = frozenset([
    "mailinator.com", "guerrillamail.com", "tempmail.com", "throwaway.email",
    "fakeinbox.com", "yopmail.com", "sharklasers.com", "guerrillamailblock.com",
    "maildrop.cc", "dispostable.com", "trashmail.com", "spamgourmet.com",
    "10minutemail.com", "spam4.me", "discard.email", "getairmail.com",
    "tempinbox.com", "trashmail.net", "mailnull.com", "spamgourmet.net",
    "crazymailing.com", "filzmail.com", "supermailer.jp", "wegwerfemail.de",
])

# Email regex (RFC 5322 simplified)
_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

# MX cache: domain → (has_mx: bool, checked_at: float)
_mx_cache: dict[str, tuple[bool, float]] = {}
_MX_CACHE_TTL = 3600  # 1 hour

# ─── Lead Scoring Weights ─────────────────────────────────────────────────────
# Each factor adds/subtracts from 0–100 base score

_SCORE_CONFIG = {
    "no_website":       +30,   # No website = very high priority target
    "old_website":      +25,   # Old/outdated website = prime candidate
    "no_booking_ai":    +20,   # No booking/AI = strong value prop
    "modern_website":   -50,   # Modern website = low priority
    "has_email":        +10,   # We found their email = actionable
    "tier1_city":       +10,   # Big city = higher revenue client potential
    "tier2_city":       +5,    # Tier 2 city still valuable
    "has_phone":        +5,    # Phone present = legitimate business
    "replied":          -100,  # Already replied = don't resend
    "sent":             -20,   # Already emailed = lower priority
    "failed":           -30,   # Failed delivery = skip
    "unsubscribed":     -200,  # Never email again
}

# Tier 1 cities (premium targets)
_TIER1_CITIES = frozenset([
    "mumbai", "delhi", "bangalore", "bengaluru", "hyderabad", "chennai",
    "kolkata", "pune", "ahmedabad", "surat", "new york", "london", "dubai",
    "toronto", "sydney", "singapore", "los angeles", "chicago", "houston",
])

_TIER2_CITIES = frozenset([
    "jaipur", "lucknow", "nagpur", "indore", "bhopal", "patna", "kanpur",
    "visakhapatnam", "agra", "nashik", "rajkot", "meerut", "vadodara",
    "coimbatore", "ludhiana", "kochi", "chandigarh", "guwahati", "bhubaneswar",
])

# ─── Subject Line A/B Pool ────────────────────────────────────────────────────
# Rotated to avoid spam filter pattern-matching on repeated identical subjects

_SUBJECT_POOLS = {
    "cold_outreach": [
        "Quick question about {business_name}'s online presence",
        "Noticed something about {business_name}'s website",
        "3 things {business_name} could improve online",
        "Your website vs your competitors — a quick note",
        "Found an issue with {business_name}'s website",
        "A quick audit for {business_name}",
        "One thing that's costing {business_name} patients online",
        "Small website change → big impact for {business_name}",
    ],
    "followup": [
        "Re: Quick website audit for {business_name}",
        "Following up — {business_name}",
        "Did my previous email reach you?",
        "One last thought on {business_name}'s website",
        "Still happy to help — {business_name}",
    ],
    "recruitment": [
        "AI automation for {business_name} — quick intro",
        "Streamline hiring at {business_name} with AI",
        "Reducing recruitment costs for {business_name}",
        "Your hiring process — a better way",
        "How {business_name} could save on recruitment",
    ],
}

# ─── Best Send Windows ────────────────────────────────────────────────────────
# Research shows: Tue–Thu, 9–11 AM or 2–4 PM local time has highest reply rates

_GOOD_WEEKDAYS = {0, 1, 2, 3, 4}   # Mon–Fri (0=Mon, 6=Sun)
_BAD_WEEKDAYS  = {5, 6}             # Sat, Sun


def is_good_send_time() -> tuple[bool, str]:
    """
    Returns (ok, reason) — whether NOW is a good time to send emails.
    
    Rules:
    - No weekends
    - No time outside 7 AM – 8 PM UTC (covers IST business hours)
    
    Returns:
        (True, "Good send window") or (False, "reason to wait")
    """
    now = datetime.utcnow()
    if now.weekday() in _BAD_WEEKDAYS:
        return False, f"Weekend ({now.strftime('%A')}) — skipping sends"
    if now.hour < 7 or now.hour >= 20:
        return False, f"Outside business hours (UTC {now.hour}:00) — skipping sends"
    return True, "Good send window"


# ─── Email Validation ─────────────────────────────────────────────────────────

def validate_email_format(email: str) -> bool:
    """
    Validate email format using RFC 5322 simplified regex.
    
    Args:
        email: Email address string to validate
        
    Returns:
        True if format is valid, False otherwise
        
    Example:
        >>> validate_email_format("dr.smith@clinic.com")
        True
        >>> validate_email_format("notanemail")
        False
    """
    return bool(_EMAIL_RE.match(email.strip()))


def is_disposable_email(email: str) -> bool:
    """
    Check if an email uses a known disposable/throwaway domain.
    
    Args:
        email: Email address to check
        
    Returns:
        True if disposable (should skip), False if legitimate
    """
    try:
        domain = email.strip().lower().split("@")[1]
        return domain in _DISPOSABLE_DOMAINS
    except (IndexError, AttributeError):
        return True  # Malformed = treat as disposable


def validate_email_mx(email: str, timeout: float = 5.0) -> tuple[bool, str]:
    """
    Validate email by checking if the domain has MX (mail exchange) records.
    
    This prevents sending to addresses at domains that can't receive email
    (reduces hard bounces, protects sender reputation).
    
    Args:
        email:   Email address to validate
        timeout: DNS lookup timeout in seconds (default: 5.0)
        
    Returns:
        (valid: bool, reason: str)
        
    Notes:
        - Results are cached for 1 hour to avoid redundant DNS lookups
        - Falls back to True (assume valid) if DNS lookup times out
          to avoid blocking the campaign on network issues
        
    Example:
        >>> ok, reason = validate_email_mx("contact@clinic.com")
        >>> if not ok:
        ...     print(f"Skipping: {reason}")
    """
    if not validate_email_format(email):
        return False, f"Invalid email format: {email}"

    if is_disposable_email(email):
        return False, f"Disposable email domain: {email.split('@')[1]}"

    domain = email.strip().lower().split("@")[1]

    # Check cache
    if domain in _mx_cache:
        has_mx, checked_at = _mx_cache[domain]
        if time.time() - checked_at < _MX_CACHE_TTL:
            if not has_mx:
                return False, f"No MX records for domain: {domain}"
            return True, "Valid (cached)"

    # DNS lookup for MX records
    try:
        # Use getaddrinfo as lightweight proxy for domain existence
        # (Full MX lookup requires dnspython — we use socket as fallback)
        try:
            import dns.resolver
            answers = dns.resolver.resolve(domain, 'MX', lifetime=timeout)
            has_mx = len(list(answers)) > 0
        except ImportError:
            # dnspython not installed — fall back to A record check
            socket.setdefaulttimeout(timeout)
            socket.getaddrinfo(domain, None)
            has_mx = True
        except Exception:
            has_mx = False

        _mx_cache[domain] = (has_mx, time.time())

        if not has_mx:
            return False, f"No MX records for domain: {domain}"
        return True, "Valid MX"

    except socket.timeout:
        logger.warning(f"MX lookup timed out for {domain} — assuming valid")
        return True, "Assumed valid (DNS timeout)"
    except Exception as e:
        logger.warning(f"MX lookup error for {domain}: {e} — assuming valid")
        return True, "Assumed valid (DNS error)"


def validate_email_full(email: str) -> tuple[bool, str]:
    """
    Full email validation: format + disposable check + MX check.
    
    Use this before adding a lead to the send queue.
    
    Args:
        email: Email address to validate fully
        
    Returns:
        (valid: bool, reason: str)
    """
    if not email or not email.strip():
        return False, "Empty email"
    ok, reason = validate_email_mx(email)
    return ok, reason


# ─── Lead Scoring ─────────────────────────────────────────────────────────────

def score_lead(
    status: str = "",
    email_status: str = "",
    location: str = "",
    has_email: bool = False,
    has_phone: bool = False,
    website: str = "",
) -> int:
    """
    Score a lead from 0–100 based on how likely they are to convert.
    
    Higher score = better prospect = send first.
    
    Scoring factors:
      +30  No website (maximum opportunity)
      +25  Old/outdated website
      +20  No booking/AI capability
      +10  Email found
      +10  Tier 1 city (Mumbai, Delhi, London, etc.)
      +5   Tier 2 city
      +5   Phone number present
      -20  Already emailed
      -30  Email delivery failed
      -50  Modern website (low priority)
      -100 Already replied (handled)
      -200 Unsubscribed (never contact)
    
    Args:
        status:       Website status ('No Website', 'Old Website', etc.)
        email_status: Email status ('Not Sent', 'Sent', 'Replied', etc.)
        location:     City name for tier scoring
        has_email:    Whether an email address was found
        has_phone:    Whether a phone number was found
        website:      Website URL (used for presence check)
        
    Returns:
        Integer score 0–100 (clamped)
        
    Example:
        >>> score = score_lead("No Website", "Not Sent", "Mumbai", True, True)
        >>> print(f"Lead score: {score}/100")
    """
    score = 50  # Base score

    # Website status
    status_lower = status.lower() if status else ""
    if "no website" in status_lower:
        score += _SCORE_CONFIG["no_website"]
    elif "old website" in status_lower:
        score += _SCORE_CONFIG["old_website"]
    elif "no booking" in status_lower or "no ai" in status_lower:
        score += _SCORE_CONFIG["no_booking_ai"]
    elif "modern" in status_lower:
        score += _SCORE_CONFIG["modern_website"]

    # Email status
    es_lower = email_status.lower() if email_status else ""
    if "replied" in es_lower:
        score += _SCORE_CONFIG["replied"]
    elif "sent" in es_lower or "follow-up" in es_lower:
        score += _SCORE_CONFIG["sent"]
    elif "failed" in es_lower:
        score += _SCORE_CONFIG["failed"]
    elif "unsubscribed" in es_lower:
        score += _SCORE_CONFIG["unsubscribed"]

    # Contactability
    if has_email:
        score += _SCORE_CONFIG["has_email"]
    if has_phone:
        score += _SCORE_CONFIG["has_phone"]

    # Location tier
    loc_lower = location.lower() if location else ""
    if any(city in loc_lower for city in _TIER1_CITIES):
        score += _SCORE_CONFIG["tier1_city"]
    elif any(city in loc_lower for city in _TIER2_CITIES):
        score += _SCORE_CONFIG["tier2_city"]

    return max(0, min(100, score))


# ─── Subject Line Rotation ────────────────────────────────────────────────────

def rotate_subject(
    category: str = "cold_outreach",
    business_name: str = "",
    seed: Optional[str] = None,
) -> str:
    """
    Return a rotated subject line from the A/B pool.
    
    Uses the email address as a seed so the same recipient always gets
    the same subject (for reply threading), but different recipients
    get different subjects (for A/B distribution).
    
    Args:
        category:      Subject pool to use ('cold_outreach', 'followup', 'recruitment')
        business_name: Name to inject into the subject template
        seed:          Seed string (use email address for consistent per-recipient selection)
        
    Returns:
        Formatted subject line string
        
    Example:
        >>> subject = rotate_subject("cold_outreach", "City Dental", "dr@citydental.com")
        >>> print(subject)
        "Quick question about City Dental's online presence"
    """
    pool = _SUBJECT_POOLS.get(category, _SUBJECT_POOLS["cold_outreach"])
    
    if seed:
        # Deterministic selection: same seed → same subject always
        idx = hash(seed) % len(pool)
    else:
        idx = random.randint(0, len(pool) - 1)
    
    template = pool[idx]
    name = business_name or "your clinic"
    
    try:
        return template.format(business_name=name)
    except (KeyError, IndexError):
        return template


# ─── Daily Run Summary Email ──────────────────────────────────────────────────

def send_run_summary(
    workflow_name: str,
    stats: dict,
    smtp_user: str,
    smtp_pass: str,
    recipient: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 587,
    errors: list[str] | None = None,
) -> bool:
    """
    Send a beautiful HTML daily run summary email to yourself after each workflow.
    
    This keeps you informed of exactly what ran, what succeeded, and what failed
    without needing to check GitHub Actions logs manually.
    
    Args:
        workflow_name: Name of the workflow ('Global Outreach', 'India Outreach', etc.)
        stats: Dictionary with keys:
               - leads_scraped (int)
               - emails_sent (int)
               - followups_sent (int)
               - replies_found (int)
               - errors (int)
               - duration_seconds (float)
               - total_in_db (int)
        smtp_user:     Gmail address to send FROM
        smtp_pass:     App password for smtp_user
        recipient:     Email to send the summary TO (usually yourself)
        smtp_host:     SMTP server (default: smtp.gmail.com)
        smtp_port:     SMTP port (default: 587)
        errors:        List of error messages to include in report
        
    Returns:
        True if summary sent successfully, False on any error
        
    Example:
        >>> ok = send_run_summary(
        ...     "India Outreach",
        ...     {"emails_sent": 18, "leads_scraped": 45, ...},
        ...     "cupboard587@gmail.com",
        ...     "app_password_here",
        ...     "aditya.airecruitment@gmail.com"
        ... )
    """
    if not smtp_user or not smtp_pass:
        logger.warning("send_run_summary: no SMTP credentials — skipping summary")
        return False

    errors = errors or []
    now = datetime.now().strftime("%d %b %Y, %I:%M %p IST")
    duration = stats.get("duration_seconds", 0)
    dur_str = f"{int(duration // 60)}m {int(duration % 60)}s" if duration else "—"

    # Build status color
    sent = stats.get("emails_sent", 0)
    if sent > 0:
        status_color = "#10b981"
        status_text  = "✅ Successful"
    elif errors:
        status_color = "#f87171"
        status_text  = "⚠️ Completed with Errors"
    else:
        status_color = "#f59e0b"
        status_text  = "ℹ️ No Emails Sent"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 28px; max-width: 600px; margin: 0 auto; }}
  .header {{ border-bottom: 2px solid {status_color}; padding-bottom: 16px; margin-bottom: 24px; }}
  .title {{ font-size: 22px; font-weight: 700; color: #f8fafc; }}
  .subtitle {{ color: #94a3b8; font-size: 13px; margin-top: 4px; }}
  .status {{ display: inline-block; background: {status_color}22; border: 1px solid {status_color}; 
             color: {status_color}; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; }}
  .metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 20px 0; }}
  .metric {{ background: #0f172a; border-radius: 8px; padding: 14px; text-align: center; }}
  .metric-val {{ font-size: 28px; font-weight: 800; color: {status_color}; }}
  .metric-label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }}
  .errors {{ background: #7f1d1d22; border: 1px solid #ef4444; border-radius: 8px; padding: 14px; margin-top: 16px; }}
  .error-title {{ color: #f87171; font-weight: 600; margin-bottom: 8px; }}
  .error-item {{ color: #fca5a5; font-size: 12px; margin: 4px 0; font-family: monospace; }}
  .footer {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid #1e293b; color: #475569; font-size: 11px; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="title">📊 {workflow_name} — Daily Report</div>
    <div class="subtitle">{now} &nbsp;·&nbsp; Duration: {dur_str}</div>
    <div style="margin-top:10px"><span class="status">{status_text}</span></div>
  </div>
  
  <div class="metrics">
    <div class="metric">
      <div class="metric-val">{stats.get('emails_sent', 0)}</div>
      <div class="metric-label">Emails Sent</div>
    </div>
    <div class="metric">
      <div class="metric-val">{stats.get('leads_scraped', 0)}</div>
      <div class="metric-label">Leads Scraped</div>
    </div>
    <div class="metric">
      <div class="metric-val">{stats.get('replies_found', 0)}</div>
      <div class="metric-label">Replies Found</div>
    </div>
    <div class="metric">
      <div class="metric-val">{stats.get('followups_sent', 0)}</div>
      <div class="metric-label">Follow-ups Sent</div>
    </div>
    <div class="metric">
      <div class="metric-val">{stats.get('total_in_db', 0)}</div>
      <div class="metric-label">Total in DB</div>
    </div>
    <div class="metric">
      <div class="metric-val">{len(errors)}</div>
      <div class="metric-label">Errors</div>
    </div>
  </div>
  
  {''.join([f'<div class="errors"><div class="error-title">⚠ Errors ({len(errors)})</div>' + 
            ''.join([f'<div class="error-item">• {e}</div>' for e in errors[:10]]) + 
            '</div>']) if errors else ''}
  
  <div class="footer">
    Sent by Global AI Outreach System &nbsp;·&nbsp; 
    <a href="https://github.com/adi-0704/global-leads-system/actions" 
       style="color:#6366f1">View Actions</a>
  </div>
</div>
</body>
</html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["From"]       = smtp_user
        msg["To"]         = recipient
        msg["Subject"]    = f"[{workflow_name}] Daily Report — {sent} emails sent · {now}"
        msg["Date"]       = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())

        logger.info(f"Run summary sent to {recipient}")
        return True

    except Exception as e:
        logger.error(f"Failed to send run summary: {e}")
        return False


# ─── Bounce Guard ─────────────────────────────────────────────────────────────

def check_bounce_guard(
    db_path: str,
    max_bounce_rate: float = 0.05,
    lookback_days: int = 7,
) -> tuple[bool, str]:
    """
    Check if bounce rate is too high — pause campaign if so.
    
    Gmail starts throttling/banning accounts when bounce rate exceeds 5%.
    This function reads the local DB to compute recent bounce rate.
    
    Args:
        db_path:          Path to leads.db SQLite file
        max_bounce_rate:  Maximum acceptable bounce rate (default: 0.05 = 5%)
        lookback_days:    How many days back to check (default: 7)
        
    Returns:
        (ok: bool, message: str)
        ok=True  means bounce rate is safe
        ok=False means campaign should pause
        
    Example:
        >>> ok, msg = check_bounce_guard("leads.db")
        >>> if not ok:
        ...     print(f"PAUSING: {msg}")
    """
    try:
        cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
        with sqlite3.connect(db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE sent_at > ?", (cutoff,)
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE email_status='Failed' AND sent_at > ?",
                (cutoff,)
            ).fetchone()[0]

        if total == 0:
            return True, "No sends in lookback window — no bounce data"

        rate = failed / total
        if rate > max_bounce_rate:
            return False, (
                f"Bounce rate {rate:.1%} exceeds {max_bounce_rate:.0%} "
                f"({failed}/{total} in last {lookback_days}d) — PAUSING campaign"
            )
        return True, f"Bounce rate OK: {rate:.1%} ({failed}/{total})"

    except sqlite3.Error as e:
        logger.warning(f"Bounce guard DB error: {e} — assuming safe")
        return True, f"DB error (assumed safe): {e}"


# ─── Warm-Up Scheduler ────────────────────────────────────────────────────────

def get_warmup_daily_limit(
    account_age_days: int,
    hard_cap: int = 50,
) -> int:
    """
    Calculate the safe daily email limit based on account warm-up schedule.
    
    New Gmail accounts should start slowly and ramp up gradually to avoid
    triggering spam filters. This follows Google's recommended warm-up curve:
    
    Day 1–3:   5 emails/day
    Day 4–7:   10 emails/day
    Day 8–14:  20 emails/day
    Day 15+:   Full limit (hard_cap)
    
    Args:
        account_age_days: How many days the sending account has been active
        hard_cap:         Maximum limit after full warm-up (default: 50)
        
    Returns:
        Safe daily limit as integer
        
    Example:
        >>> limit = get_warmup_daily_limit(account_age_days=10, hard_cap=50)
        >>> print(f"Today's limit: {limit}")  # → 20
    """
    if account_age_days < 3:
        return min(5, hard_cap)
    elif account_age_days < 7:
        return min(10, hard_cap)
    elif account_age_days < 14:
        return min(20, hard_cap)
    else:
        return hard_cap


# ─── Quick Utilities ─────────────────────────────────────────────────────────

def extract_first_name(email: str) -> str:
    """
    Extract a probable first name from an email address for personalization.
    
    Attempts to parse the local part of the email address and extract a
    capitalized first name. Falls back to "there" if unable to parse.
    
    Args:
        email: Email address string
        
    Returns:
        Probable first name (e.g. "John") or "there" as fallback
        
    Example:
        >>> extract_first_name("dr.john.smith@clinic.com")
        "John"
        >>> extract_first_name("info@genericclinic.com")
        "there"
    """
    try:
        local = email.split("@")[0].lower()
        # Strip common prefixes
        for prefix in ["dr.", "dr", "info", "admin", "contact", "hello", "support", "clinic", "dental"]:
            if local.startswith(prefix):
                local = local[len(prefix):].lstrip("._-")
        # Take first segment split by . _ -
        parts = re.split(r"[._\-]", local)
        name = next((p for p in parts if len(p) > 2 and p.isalpha()), "")
        return name.capitalize() if name else "there"
    except Exception:
        return "there"


def format_send_stats(
    sent: int = 0,
    scraped: int = 0,
    followups: int = 0,
    replies: int = 0,
    errors: int = 0,
    duration: float = 0,
) -> str:
    """
    Format run statistics as a printable summary string.
    
    Args:
        sent:      Number of new emails sent
        scraped:   Number of new leads scraped
        followups: Number of follow-up emails sent
        replies:   Number of new replies found
        errors:    Number of errors encountered
        duration:  Total run duration in seconds
        
    Returns:
        Formatted multi-line string summary
    """
    dur_str = f"{int(duration // 60)}m {int(duration % 60)}s" if duration else "—"
    return (
        f"\n{'='*50}\n"
        f"  CAMPAIGN RUN COMPLETE\n"
        f"{'='*50}\n"
        f"  ✉  Emails Sent:     {sent}\n"
        f"  🔄 Follow-ups:      {followups}\n"
        f"  📋 Leads Scraped:   {scraped}\n"
        f"  💬 Replies Found:   {replies}\n"
        f"  ❌ Errors:          {errors}\n"
        f"  ⏱  Duration:        {dur_str}\n"
        f"{'='*50}\n"
    )
