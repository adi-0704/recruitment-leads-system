"""
email_finder.py — Email Discovery from Business Websites
=========================================================
Visits each business website URL and attempts to find an email address
by scanning the page source for common email patterns.

Strategy (in order):
  1. Scan the homepage HTML for mailto: links.
  2. Scan visible text with a broad regex pattern.
  3. Check /contact, /about, /contact-us sub-pages for the email.

Author  : Google Maps Scraper Project
Requires: requests
"""

import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex pattern that matches the vast majority of real-world email addresses
_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Regex pattern for obfuscated emails, e.g. "info [at] domain.com", "info (at) domain.com", "info at domain dot com"
_OBFUSCATED_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+(?:\s*\[at\]\s*|\s*\(\s*at\s*\)\s*|\s+at\s+)[a-zA-Z0-9.\-]+(?:\s*\[dot\]\s*|\s*\(\s*dot\s*\)\s*|\s+dot\s+)[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Sub-pages most likely to contain contact info
_CONTACT_PATHS = [
    "/contact", "/contact-us", "/about", "/about-us", "/reach-us",
    "/contact-us/", "/contact/", "/about-us/", "/about/",
    "/info", "/info/", "/help", "/help/", "/privacy", "/privacy-policy"
]

# Domains we never want to return (noise/common false-positives)
_IGNORED_DOMAINS = {
    "example.com",
    "sentry.io",
    "wix.com",
    "wordpress.com",
    "google.com",
    "schema.org",
    "w3.org",
}

# HTTP request timeout in seconds
_TIMEOUT = 5

# Default headers to mimic a real browser visit
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


# ---------------------------------------------------------------------------
# EmailFinder class
# ---------------------------------------------------------------------------

class EmailFinder:
    """
    Discovers email addresses by visiting business website URLs.

    Usage
    -----
    >>> finder = EmailFinder(delay=0.5)
    >>> email = finder.find("https://example-business.com")
    >>> print(email)   # "info@example-business.com" or ""

    Parameters
    ----------
    delay : float
        Seconds to wait between HTTP requests (be polite to servers).
        Default: 0.5 s.
    check_contact_pages : bool
        If True and no email found on homepage, also check common
        contact/about sub-pages. Slower but more thorough. Default: True.
    """

    def __init__(self, delay: float = 0.5, check_contact_pages: bool = True) -> None:
        self.delay = delay
        self.check_contact_pages = check_contact_pages
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    # ── Public API ───────────────────────────────────────────────────────────

    def find(self, website_url: str) -> str:
        """
        Return the first email address found on the given website, or "".

        Parameters
        ----------
        website_url : str
            Full URL (with scheme) of the business website.

        Returns
        -------
        str
            Email address string, or empty string if none found.
        """
        if not website_url:
            return ""

        # Normalise URL — ensure it has a scheme
        if not website_url.startswith(("http://", "https://")):
            website_url = "https://" + website_url

        # ── Step 1: Scan homepage ─────────────────────────────────────────
        html = self._fetch(website_url)
        dynamic_urls = []
        if html:
            email = self._extract_email(html, website_url)
            if email:
                return email

            # Dynamic link harvesting: extract contact/about links from homepage
            try:
                hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
                base_dom = self._base_domain(website_url).lower()
                for href in hrefs:
                    href = href.strip()
                    if not href or href.startswith(("#", "javascript:", "tel:", "mailto:")):
                        continue
                    resolved_url = urljoin(website_url, href)
                    if self._base_domain(resolved_url).lower() == base_dom:
                        url_lower = resolved_url.lower()
                        # check if URL looks like a contact or info page
                        if any(k in url_lower for k in ["contact", "about", "reach", "touch", "info", "team", "doctor", "staff", "clinic", "detail"]):
                            if resolved_url not in dynamic_urls:
                                dynamic_urls.append(resolved_url)
            except Exception:
                pass

        # ── Step 2: Check contact / about sub-pages ───────────────────────
        if self.check_contact_pages:
            base = self._base_url(website_url)
            
            # Combine dynamic URLs with hardcoded fallback paths
            urls_to_check = list(dynamic_urls)
            for path in _CONTACT_PATHS:
                sub_url = urljoin(base, path)
                if sub_url not in urls_to_check:
                    urls_to_check.append(sub_url)

            # Limit checking to first 5 pages to avoid excessive request delays
            for sub_url in urls_to_check[:5]:
                time.sleep(self.delay)
                html = self._fetch(sub_url)
                if html:
                    email = self._extract_email(html, website_url)
                    if email:
                        return email

        return ""

    def enrich(self, records: list) -> list:
        """
        Enrich a list of BusinessRecord objects by filling in the email field.

        Parameters
        ----------
        records : list[BusinessRecord]
            Records to enrich in-place.

        Returns
        -------
        list[BusinessRecord]
            The same list with email fields populated where possible.
        """
        for record in records:
            if record.website and not record.email:
                record.email = self.find(record.website)
                time.sleep(self.delay)
        return records

    # ── Private helpers ──────────────────────────────────────────────────────

    def _fetch(self, url: str) -> Optional[str]:
        """
        Perform a GET request and return the response text, or None on error.
        Silently swallows all network / HTTP errors.
        Retries without SSL verification if SSLError is encountered.
        """
        try:
            resp = self._session.get(url, timeout=_TIMEOUT, allow_redirects=True)
            if resp.ok:
                return resp.text
        except requests.exceptions.SSLError:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                resp = self._session.get(url, timeout=_TIMEOUT, allow_redirects=True, verify=False)
                if resp.ok:
                    return resp.text
            except Exception:
                pass
        except Exception:
            pass
        return None

    def _extract_email(self, html: str, source_url: str) -> str:
        """
        Scan *html* for email addresses, filtering out noise.

        Priority:
          1. mailto: href values (most reliable)
          2. Regex matches anywhere in the page source
          3. Obfuscated email pattern matches (e.g. [at], (at), [dot], etc.)

        Parameters
        ----------
        html       : Raw page HTML string.
        source_url : The URL the HTML came from (used to skip self-domain noise).

        Returns
        -------
        str — First valid email found, or "".
        """
        source_domain = self._base_domain(source_url)

        # ── Priority 1: mailto: links ─────────────────────────────────────
        import urllib.parse
        mailto_pattern = re.compile(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})')
        for match in mailto_pattern.finditer(html):
            email = urllib.parse.unquote(match.group(1)).strip().lower()
            if self._is_valid(email):
                return email

        # ── Priority 2: Generic regex scan ───────────────────────────────
        for match in _EMAIL_PATTERN.finditer(html):
            email = urllib.parse.unquote(match.group(0)).strip().lower()
            if self._is_valid(email):
                return email

        # ── Priority 3: Obfuscated regex scan ─────────────────────────────
        for match in _OBFUSCATED_PATTERN.finditer(html):
            raw_match = urllib.parse.unquote(match.group(0)).strip().lower()
            decoded = re.sub(r"\s*(?:\[at\]|\(\s*at\s*\)|at)\s*", "@", raw_match, flags=re.IGNORECASE)
            decoded = re.sub(r"\s*(?:\[dot\]|\(\s*dot\s*\)|dot)\s*", ".", decoded, flags=re.IGNORECASE)
            if self._is_valid(decoded):
                return decoded

        return ""

    @staticmethod
    def _is_valid(email: str) -> bool:
        """
        Return True if *email* passes basic sanity checks.
        Filters out common noise like image filenames (.png@, etc.) and
        well-known false-positive domains.
        """
        if not email or "@" not in email:
            return False

        domain = email.split("@")[-1].lower()
        if domain in _IGNORED_DOMAINS:
            return False

        # Reject emails ending in image / asset extensions
        bad_extensions = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
        if any(email.endswith(ext) for ext in bad_extensions):
            return False

        # Must have at least one dot in domain
        if "." not in domain:
            return False

        return True

    @staticmethod
    def _base_url(url: str) -> str:
        """Return scheme + netloc from a URL, e.g. 'https://example.com'."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _base_domain(url: str) -> str:
        """Return just the netloc (domain) from a URL."""
        return urlparse(url).netloc
