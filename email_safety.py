"""
email_safety.py — Anti-Spam, Deliverability & Resilience Module
================================================================
Shared across all 3 outreach systems:
  - Global Outreach (cupboard587@gmail.com)
  - India Outreach  (cupboard587@gmail.com)
  - Recruitment     (aditya.airecruitment@gmail.com)

Anti-Spam Features:
  ✓ RFC-compliant headers (Message-ID, Date, List-Unsubscribe)
  ✓ Random jitter delays (60–120s between emails)
  ✓ Hard daily cap enforcement
  ✓ Unsubscribe footer in every email body
  ✓ Retry-with-backoff on transient SMTP errors
  ✓ Marks permanent failures so they're never retried
  ✓ Resilient scraper wrapper — never crashes the full run
"""

import os
import re
import time
import random
import smtplib
import sqlite3
import socket
import traceback
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid

# ─── Hard daily sending limit (safety guard across all accounts) ────────────
GLOBAL_DAILY_HARD_CAP = 50   # Global & India outreach
RECRUIT_DAILY_HARD_CAP = 50  # Recruitment outreach

# ─── Delay settings (seconds) ───────────────────────────────────────────────
SEND_DELAY_MIN = 60    # Min seconds between sends
SEND_DELAY_MAX = 120   # Max seconds between sends (random jitter)

# ─── Unsubscribe footer (appended to every email) ───────────────────────────
UNSUBSCRIBE_FOOTER = (
    "\n\n---\n"
    "You received this email because your business was found via Google Maps.\n"
    "To stop receiving emails, simply reply with 'Unsubscribe' in the subject line.\n"
    "Aditya Tyagi | AI & Automation Engineer | aditya.airecruitment@gmail.com"
)

# ─── Permanent failure codes — never retry these ───────────────────────────
PERMANENT_SMTP_ERRORS = {
    550, 551, 552, 553, 554,  # Invalid/non-existent mailbox
    421,  # Service unavailable (domain issue)
}


def safe_delay(min_s: int = SEND_DELAY_MIN, max_s: int = SEND_DELAY_MAX) -> None:
    """Random jitter delay between emails — looks human, avoids rate limits."""
    delay = random.randint(min_s, max_s)
    print(f"  [Safety] Waiting {delay}s before next email (anti-spam jitter)...")
    time.sleep(delay)


def append_unsubscribe(body: str) -> str:
    """Append mandatory unsubscribe footer to email body."""
    if "Unsubscribe" in body or "unsubscribe" in body:
        return body  # Already has one
    return body.rstrip() + UNSUBSCRIBE_FOOTER


def build_safe_message(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    sender_user: str,
) -> MIMEMultipart:
    """
    Build a properly-headered email message that Gmail won't flag as spam.

    Key headers added:
      - Message-ID  : unique per email (RFC 5322)
      - Date        : current timestamp (RFC 2822)
      - List-Unsubscribe : Google requirement for bulk senders
      - MIME-Version, Content-Type set correctly
    """
    msg = MIMEMultipart("alternative")
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Subject"] = subject
    msg["Date"]    = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_user.split("@")[-1] if "@" in sender_user else "gmail.com")

    # List-Unsubscribe (Gmail/Yahoo requirement for bulk senders since 2024)
    msg["List-Unsubscribe"] = f"<mailto:{sender_user}?subject=Unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Looks more like a personal email, not a mass mailer
    msg["X-Priority"] = "3"       # Normal priority
    msg["Importance"] = "Normal"

    # Append unsubscribe footer to body
    safe_body = append_unsubscribe(body)

    # Attach as plain text (simple = less likely to be spam-scored)
    msg.attach(MIMEText(safe_body, "plain", "utf-8"))

    return msg


def send_safe_email(
    recipient: str,
    subject: str,
    body: str,
    smtp_user: str,
    smtp_pass: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 587,
    smtp_from: str = "",
    max_retries: int = 2,
) -> tuple[bool, str]:
    """
    Send a single email with full anti-spam headers and retry-on-transient-error.

    Returns (success: bool, account_used: str)

    Raises:
      - smtplib.SMTPAuthenticationError if credentials are wrong
      - RuntimeError if all retries fail on a permanent error
    """
    sender    = smtp_from.strip() or smtp_user.strip()
    user      = smtp_user.strip()
    pwd       = smtp_pass.strip()

    if not user or not pwd:
        raise RuntimeError("SMTP credentials are empty — cannot send email.")

    msg = build_safe_message(
        sender    = sender,
        recipient = recipient,
        subject   = subject,
        body      = body,
        sender_user = user,
    )

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(user, pwd)
                server.sendmail(sender, [recipient], msg.as_string())
            return True, user

        except smtplib.SMTPAuthenticationError:
            # Credentials are wrong — fail immediately, no retry
            raise

        except smtplib.SMTPRecipientsRefused as e:
            # Permanent bounce — mark and stop
            code = list(e.recipients.values())[0][0] if e.recipients else 0
            print(f"  [Email Safety] Permanent bounce ({code}) for {recipient}. Skipping.")
            raise

        except smtplib.SMTPException as e:
            last_err = e
            code = getattr(e, 'smtp_code', 0)
            if code in PERMANENT_SMTP_ERRORS:
                print(f"  [Email Safety] Permanent SMTP error {code}: {e}")
                raise
            # Transient error — wait and retry
            wait = 30 * attempt
            print(f"  [Email Safety] Transient SMTP error (attempt {attempt}/{max_retries}): {e}. Retrying in {wait}s...")
            if attempt < max_retries:
                time.sleep(wait)

        except (socket.timeout, OSError) as e:
            last_err = e
            wait = 30 * attempt
            print(f"  [Email Safety] Network error (attempt {attempt}/{max_retries}): {e}. Retrying in {wait}s...")
            if attempt < max_retries:
                time.sleep(wait)

        except Exception as e:
            last_err = e
            print(f"  [Email Safety] Unexpected error: {e}")
            break

    raise RuntimeError(f"Failed to send to {recipient} after {max_retries} attempts: {last_err}")


def get_smtp_creds(
    config: dict,
    default_user: str = "",
    default_pass: str = "",
    default_host: str = "smtp.gmail.com",
    default_port: int = 587,
    default_from: str = "",
) -> tuple[str, str, str, int, str]:
    """
    Resolve SMTP credentials in priority order:
      1. Environment variables (set by GitHub Actions secrets)
      2. config.json values
      3. Hardcoded defaults (last resort)

    Returns: (user, password, host, port, from_addr)
    """
    user = (
        os.environ.get("SMTP_USER", "").strip()
        or config.get("smtp_user", "").strip()
        or default_user.strip()
    )
    pwd = (
        os.environ.get("SMTP_PASSWORD", "").strip()
        or config.get("smtp_password", "").strip()
        or default_pass.strip()
    )
    host = (
        os.environ.get("SMTP_HOST", "").strip()
        or config.get("smtp_host", "").strip()
        or default_host
    )
    try:
        port = int(os.environ.get("SMTP_PORT", "") or config.get("smtp_port", default_port))
    except (ValueError, TypeError):
        port = default_port

    from_addr = (
        os.environ.get("SMTP_FROM", "").strip()
        or config.get("smtp_from", "").strip()
        or user
    )

    return user, pwd, host, port, from_addr


def check_daily_cap(db_path: str, cap: int, today: str | None = None) -> tuple[int, bool]:
    """
    Count emails sent + follow-ups sent today.
    Returns (count_today, cap_reached).
    """
    if today is None:
        today = date.today().isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE sent_at LIKE ? OR followup_sent_at LIKE ?",
                (f"{today}%", f"{today}%")
            ).fetchone()[0]
        return count, count >= cap
    except sqlite3.Error as e:
        print(f"  [Safety] DB cap check failed: {e}")
        return 0, False


def mark_unsubscribed(db_path: str, email_addr: str) -> None:
    """Mark a lead as unsubscribed so they never get emailed again."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE leads SET email_status='Unsubscribed' WHERE email=?",
                (email_addr,)
            )
            conn.commit()
        print(f"  [Safety] Marked {email_addr} as Unsubscribed.")
    except sqlite3.Error as e:
        print(f"  [Safety] Could not mark unsubscribed: {e}")


def resilient_scrape(scrape_fn, *args, **kwargs):
    """
    Wrap a scraping call so it NEVER crashes the whole run.
    Returns (result, error_or_None).
    """
    try:
        result = scrape_fn(*args, **kwargs)
        return result, None
    except Exception as e:
        print(f"  [Resilience] Scraping failed (non-fatal): {type(e).__name__}: {e}")
        traceback.print_exc()
        return None, e


def resilient_send(send_fn, *args, max_retries: int = 2, **kwargs):
    """
    Wrap a single email send so it NEVER crashes the whole campaign run.
    Returns (success: bool, error_or_None).
    """
    try:
        result = send_fn(*args, **kwargs)
        return result, None
    except smtplib.SMTPAuthenticationError as e:
        print(f"  [Resilience] AUTH FAILED — check your Gmail App Password! {e}")
        return False, e
    except Exception as e:
        print(f"  [Resilience] Email send failed (non-fatal, continuing): {type(e).__name__}: {e}")
        return False, e
