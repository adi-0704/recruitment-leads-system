# -*- coding: utf-8 -*-
"""
dedup_engine.py — Cross-Workflow Deduplication Engine
======================================================
Maintains a SHARED SQLite database that tracks every email address ever
contacted across ALL 3 outreach workflows (Global, India, Recruitment).

WHY THIS EXISTS
---------------
Without this, the same doctor could receive outreach emails from:
  - Global workflow   (cupboard587@gmail.com)
  - India workflow    (cupboard587@gmail.com)
  - Recruitment workflow (aditya.airecruitment@gmail.com)

That damages reputation and can trigger Gmail abuse reports.

GRAPH POSITION IN DAG
---------------------
  [Lead Scorer] -> [DEDUP CHECK] -> [Email Sender]

DATABASE SCHEMA
---------------
  Table: contacted
    email TEXT PK, workflow TEXT, contacted_at TEXT,
    email_status TEXT, sent_by TEXT

  Table: unsubscribed
    email TEXT PK, reason TEXT, added_at TEXT

Usage::

    from dedup_engine import dedup          # module-level singleton

    if dedup.is_sendable("dr@clinic.com"):
        send_email(lead)
        dedup.mark_contacted("dr@clinic.com", "India Outreach", smtp_user)

Author: Aditya Tyagi
Version: 2.0.0
"""

import os
import re
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

logger = logging.getLogger("dedup_engine")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s [DEDUP] %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

# Shared dedup DB lives at the project root (accessible to all 3 workflows)
_DEFAULT_DEDUP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "global_dedup.db"
)

# Keywords that indicate unsubscribe intent in a reply
_UNSUB_KEYWORDS = [
    "unsubscribe", "remove me", "stop emailing", "opt out", "opt-out",
    "do not contact", "don't contact", "take me off", "please remove",
    "not interested", "stop sending", "no thank you",
]

# SQL: create all tables + indexes
_DDL = """
CREATE TABLE IF NOT EXISTS contacted (
    email        TEXT PRIMARY KEY COLLATE NOCASE,
    workflow     TEXT NOT NULL,
    contacted_at TEXT NOT NULL,
    email_status TEXT NOT NULL DEFAULT 'Sent',
    sent_by      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS unsubscribed (
    email    TEXT PRIMARY KEY COLLATE NOCASE,
    reason   TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacted_workflow ON contacted(workflow);
CREATE INDEX IF NOT EXISTS idx_contacted_at       ON contacted(contacted_at);
"""


class DedupEngine:
    """
    Cross-workflow email deduplication and unsubscribe list manager.

    Uses SQLite under the hood. For file-based DBs, WAL mode is enabled
    for safe concurrent access from multiple processes. For ':memory:'
    (used in tests), a single persistent connection is kept alive.

    Attributes:
        db_path: Absolute path to the shared dedup SQLite file.

    Example::

        dedup = DedupEngine()
        safe = [l for l in leads if dedup.is_sendable(l['email'])]
        for lead in safe:
            send_email(lead)
            dedup.mark_contacted(lead['email'], "Global Outreach", smtp_user)
    """

    def __init__(self, db_path: str = _DEFAULT_DEDUP_PATH):
        """
        Initialise the dedup engine.

        Args:
            db_path: Path to the SQLite file.
                     Use ':memory:' for testing (data is not persisted).
        """
        self.db_path   = db_path
        self._is_mem   = (db_path == ":memory:")
        # Keep one persistent connection for :memory: (connection-scoped DB)
        self._mem_conn: Optional[sqlite3.Connection] = (
            sqlite3.connect(":memory:", check_same_thread=False)
            if self._is_mem else None
        )
        self._init_db()

    # ── Connection management ─────────────────────────────────────────────────

    @contextmanager
    def _conn(self, timeout: float = 10) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager: yields an open SQLite connection.

        For ':memory:' DBs, reuses the single persistent connection (no close).
        For file-based DBs, opens a new connection and closes it on exit.
        """
        if self._is_mem:
            yield self._mem_conn  # type: ignore[misc]
        else:
            conn = sqlite3.connect(self.db_path, timeout=timeout)
            try:
                yield conn
            finally:
                conn.close()

    def _init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self._conn() as conn:
            if not self._is_mem:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_DDL)
            conn.commit()
        logger.info("Dedup DB ready: %s", self.db_path)

    # ── Core checks ───────────────────────────────────────────────────────────

    def is_contacted(self, email: str) -> bool:
        """
        Return True if this email was previously contacted by ANY workflow.

        Args:
            email: Email address to check (case-insensitive).

        Returns:
            True  -> already contacted, skip this lead.
            False -> never contacted, safe to send.
        """
        if not email or not email.strip():
            return True   # treat empty as already-contacted (safe default)
        try:
            with self._conn(5) as conn:
                row = conn.execute(
                    "SELECT 1 FROM contacted WHERE email=? LIMIT 1",
                    (email.strip().lower(),)
                ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            logger.warning("is_contacted error for %s: %s — assuming safe", email, exc)
            return False

    def is_unsubscribed(self, email: str) -> bool:
        """
        Return True if this email is on the global unsubscribe list.

        Args:
            email: Email address to check.

        Returns:
            True  -> unsubscribed, NEVER email again.
            False -> not unsubscribed.
        """
        if not email or not email.strip():
            return False
        try:
            with self._conn(5) as conn:
                row = conn.execute(
                    "SELECT 1 FROM unsubscribed WHERE email=? LIMIT 1",
                    (email.strip().lower(),)
                ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            logger.warning("is_unsubscribed error for %s: %s", email, exc)
            return False

    def is_sendable(self, email: str) -> bool:
        """
        Combined gate: True only if NOT contacted AND NOT unsubscribed.

        Use this as the single check before adding a lead to the send queue.

        Args:
            email: Email address to check.

        Returns:
            True  -> safe to send.
            False -> skip (already contacted or unsubscribed).

        Example::

            send_queue = [l for l in leads if dedup.is_sendable(l['email'])]
        """
        return not self.is_unsubscribed(email) and not self.is_contacted(email)

    # ── Write operations ──────────────────────────────────────────────────────

    def mark_contacted(
        self,
        email:        str,
        workflow:     str,
        sent_by:      str = "",
        email_status: str = "Sent",
    ) -> bool:
        """
        Record a successful email send. Call immediately after SMTP success.

        Uses INSERT OR REPLACE so re-sends update the record rather than error.

        Args:
            email:        Recipient email (case-insensitive storage).
            workflow:     Workflow name ('Global Outreach', 'India Outreach', etc.)
            sent_by:      SMTP account used ('cupboard587@gmail.com', etc.)
            email_status: Status to record ('Sent', 'Follow-Up Sent', etc.)

        Returns:
            True if recorded, False on DB error.
        """
        if not email or not email.strip():
            return False
        try:
            with self._conn(10) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO contacted
                       (email, workflow, contacted_at, email_status, sent_by)
                       VALUES (?,?,?,?,?)""",
                    (
                        email.strip().lower(),
                        workflow,
                        datetime.utcnow().isoformat(),
                        email_status,
                        sent_by,
                    ),
                )
                conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.error("mark_contacted failed for %s: %s", email, exc)
            return False

    def add_unsubscribe(self, email: str, reason: str = "") -> bool:
        """
        Add an email to the permanent global unsubscribe list.

        This blocks future sends from ALL 3 workflows.

        Args:
            email:  Email to block permanently.
            reason: Human-readable reason ('reply: "stop emailing"').

        Returns:
            True if added, False on error.
        """
        if not email or not email.strip():
            return False
        try:
            with self._conn(10) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO unsubscribed (email,reason,added_at) VALUES (?,?,?)",
                    (email.strip().lower(), reason, datetime.utcnow().isoformat()),
                )
                conn.commit()
            logger.info("Unsubscribed: %s (%s)", email, reason)
            return True
        except sqlite3.Error as exc:
            logger.error("add_unsubscribe failed for %s: %s", email, exc)
            return False

    # ── Reply scanning ────────────────────────────────────────────────────────

    def scan_reply_for_unsubscribe(self, email: str, reply_body: str) -> bool:
        """
        Scan a reply body for unsubscribe-intent keywords.

        If found, automatically records the unsubscribe.

        Args:
            email:      Sender's email.
            reply_body: Plain-text body of the inbound reply.

        Returns:
            True if unsubscribe intent detected (and recorded).
            False if no unsubscribe intent found.
        """
        if not reply_body:
            return False
        body_lower = reply_body.lower()
        for kw in _UNSUB_KEYWORDS:
            if kw in body_lower:
                reason = "auto-detected keyword: '%s'" % kw
                self.add_unsubscribe(email, reason)
                return True
        return False

    # ── Bulk operations ───────────────────────────────────────────────────────

    def filter_sendable(
        self, emails: list
    ) -> tuple:
        """
        Split a list of emails into (sendable, skipped) using a single DB query.

        Much faster than calling is_sendable() in a loop for large batches.

        Args:
            emails: List of email address strings.

        Returns:
            (sendable: list, skipped: list)

        Example::

            ok, skip = dedup.filter_sendable([l['email'] for l in leads])
            print("Sending to %d, skipping %d" % (len(ok), len(skip)))
        """
        if not emails:
            return [], []

        cleaned = [e.strip().lower() for e in emails if e and e.strip()]

        try:
            ph = ",".join("?" * len(cleaned))
            with self._conn(10) as conn:
                contacted = {
                    r[0] for r in conn.execute(
                        "SELECT email FROM contacted WHERE email IN (%s)" % ph, cleaned
                    )
                }
                unsubbed = {
                    r[0] for r in conn.execute(
                        "SELECT email FROM unsubscribed WHERE email IN (%s)" % ph, cleaned
                    )
                }
            blocked  = contacted | unsubbed
            sendable = [e for e in emails if e.strip().lower() not in blocked]
            skipped  = [e for e in emails if e.strip().lower() in blocked]
            return sendable, skipped
        except sqlite3.Error as exc:
            logger.warning("filter_sendable error: %s — returning all as sendable", exc)
            return emails, []

    # ── Statistics ────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        Return summary stats from the dedup database.

        Returns:
            {
                'total_contacted':    int,
                'total_unsubscribed': int,
                'today_count':        int,
                'by_workflow':        {workflow_name: count}
            }
        """
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            with self._conn(5) as conn:
                total   = conn.execute("SELECT COUNT(*) FROM contacted").fetchone()[0]
                unsub   = conn.execute("SELECT COUNT(*) FROM unsubscribed").fetchone()[0]
                today_n = conn.execute(
                    "SELECT COUNT(*) FROM contacted WHERE contacted_at LIKE ?",
                    (today + "%",)
                ).fetchone()[0]
                wf_rows = conn.execute(
                    "SELECT workflow, COUNT(*) FROM contacted GROUP BY workflow"
                ).fetchall()
            return {
                "total_contacted":    total,
                "total_unsubscribed": unsub,
                "today_count":        today_n,
                "by_workflow":        {r[0]: r[1] for r in wf_rows},
            }
        except sqlite3.Error as exc:
            logger.error("get_stats error: %s", exc)
            return {}

    def export_unsubscribe_list(self, output_path: str = "unsubscribe_list.txt") -> int:
        """
        Export the full unsubscribe list to a plain-text file.

        Args:
            output_path: Output file path.

        Returns:
            Number of entries written.
        """
        try:
            with self._conn(5) as conn:
                rows = conn.execute(
                    "SELECT email, reason, added_at FROM unsubscribed ORDER BY added_at DESC"
                ).fetchall()
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("# Global Unsubscribe List — %s\n" % datetime.utcnow().isoformat())
                f.write("# %d entries\n\n" % len(rows))
                for email, reason, added_at in rows:
                    f.write("%s  # %s (%s)\n" % (email, reason, added_at[:10]))
            logger.info("Exported %d unsubscribes to %s", len(rows), output_path)
            return len(rows)
        except Exception as exc:
            logger.error("export_unsubscribe_list failed: %s", exc)
            return 0


# ── Module-level singleton ────────────────────────────────────────────────────
# Import this in all 3 outreach scripts:
#   from dedup_engine import dedup

dedup = DedupEngine()


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Running DedupEngine self-tests...")
    e = DedupEngine(":memory:")

    # 1. mark + check
    assert not e.is_contacted("test@clinic.com"), "Should not be contacted yet"
    ok = e.mark_contacted("test@clinic.com", "Global Outreach", "sender@gmail.com")
    assert ok, "mark_contacted should return True"
    assert e.is_contacted("test@clinic.com"), "Should now be contacted"

    # 2. unsubscribe
    assert not e.is_unsubscribed("unsub@clinic.com")
    e.add_unsubscribe("unsub@clinic.com", "test reason")
    assert e.is_unsubscribed("unsub@clinic.com")
    assert not e.is_sendable("unsub@clinic.com")

    # 3. reply scan auto-unsubscribe
    detected = e.scan_reply_for_unsubscribe(
        "angry@clinic.com",
        "Please stop emailing me and remove me from your list."
    )
    assert detected, "Should detect unsubscribe intent"
    assert e.is_unsubscribed("angry@clinic.com")

    # 4. bulk filter
    sendable, skipped = e.filter_sendable([
        "test@clinic.com",        # already contacted
        "new@clinic.com",         # never contacted -> sendable
        "unsub@clinic.com",       # unsubscribed
        "angry@clinic.com",       # auto-unsubscribed
    ])
    assert "new@clinic.com" in sendable,     "new should be sendable"
    assert "test@clinic.com" in skipped,     "test should be skipped (contacted)"
    assert "unsub@clinic.com" in skipped,    "unsub should be skipped"
    assert "angry@clinic.com" in skipped,    "angry should be skipped"

    # 5. stats
    stats = e.get_stats()
    assert stats["total_contacted"] == 1,    "Should be 1 contacted"
    assert stats["total_unsubscribed"] == 2, "Should be 2 unsubscribed"

    print("ALL SELF-TESTS PASSED")
    print("Stats:", stats)
