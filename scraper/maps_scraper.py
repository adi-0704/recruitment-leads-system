"""
maps_scraper.py — Core Google Maps Scraper
==========================================
Uses Playwright browser automation to search Google Maps and extract:
  - Business name
  - Phone number
  - Website URL
  - Address

Note: Emails are NOT shown on Google Maps; use EmailFinder (email_finder.py)
      to discover them by visiting each business website afterward.

Author  : Google Maps Scraper Project
Requires: playwright (install with `pip install playwright && playwright install chromium`)
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Playwright,
)


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class BusinessRecord:
    """
    Represents a single scraped business listing.

    Attributes:
        name       : Business name as shown on Google Maps.
        phone      : Primary phone number (may be empty if not listed).
        email      : Email address discovered from the business website.
                     Empty string if not found or no website available.
        website    : Business website URL (may be empty if not listed).
        address    : Full street address as shown on Google Maps.
        scraped_at : ISO-8601 timestamp of when this record was created.
    """
    name: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    address: str = ""
    specialty: str = ""
    scraped_at: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    def to_dict(self) -> dict:
        """Return the record as a plain dictionary (suitable for JSON/CSV)."""
        return {
            "name": self.name,
            "phone": self.phone,
            "email": self.email,
            "website": self.website,
            "address": self.address,
            "scraped_at": self.scraped_at,
        }


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class GoogleMapsScraper:
    """
    Async context-manager-based scraper for Google Maps.

    Usage
    -----
    >>> import asyncio
    >>> from scraper import GoogleMapsScraper
    >>>
    >>> async def run():
    ...     async with GoogleMapsScraper(headless=True) as scraper:
    ...         async for record in scraper.search("dentists", "Mumbai", max_results=50):
    ...             print(record.name, record.phone)
    >>>
    >>> asyncio.run(run())

    Parameters
    ----------
    headless : bool
        Run Chromium in headless mode (no visible window). Default: True.
    delay_ms : int
        Milliseconds to wait after clicking a listing before extracting data.
        Higher values are safer but slower. Default: 1800 ms.
    """

    _BASE_URL = "https://www.google.com/maps/search/"

    # ── Selector sets (ordered by priority) ─────────────────────────────────
    # Google frequently changes class names; we use stable attribute selectors.

    _LISTING_SELECTORS = [
        'a[href*="/maps/place/"]',
    ]

    _NAME_SELECTORS = [
        "h1.DUwDvf",
        "h1.fontHeadlineLarge",
        "h1",
    ]

    _ADDRESS_SELECTORS = [
        'button[data-item-id="address"]',
        '[data-item-id="address"]',
        'button[data-tooltip="Copy address"]',
        'div[class*="rogA2c"]',   # fallback observed selector
    ]

    _PHONE_SELECTORS = [
        '[data-item-id^="phone:tel:"]',
        'button[data-tooltip="Copy phone number"]',
        'a[href^="tel:"]',
    ]

    _WEBSITE_SELECTORS = [
        'a[data-item-id="authority"]',
        'a[href*="http"][data-item-id]',
    ]

    # ────────────────────────────────────────────────────────────────────────

    def __init__(self, headless: bool = True, delay_ms: int = 1800) -> None:
        self.headless = headless
        self.delay_ms = delay_ms
        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "GoogleMapsScraper":
        self._pw = await async_playwright().start()
        browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        # Mask webdriver flag to reduce bot detection
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.browser.close()
        if self._pw:
            await self._pw.stop()

    # ── Public API ───────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        location: str = "",
        max_results: int = 100,
        progress_callback=None,
    ) -> AsyncGenerator[BusinessRecord, None]:
        """
        Search Google Maps for businesses matching *query* and *location*.

        Parameters
        ----------
        query          : Search term, e.g. "dentists", "pizza restaurants".
        location       : City/area filter, e.g. "Mumbai", "New York, NY".
        max_results    : Stop after yielding this many records.
        progress_callback : Optional async callable(count, total) for UI updates.

        Yields
        ------
        BusinessRecord objects (one per unique listing found).
        """
        assert self._context, "Use 'async with GoogleMapsScraper() as s:' first."

        full_query = f"{query} {location}".strip()
        page = await self._context.new_page()

        try:
            search_url = self._BASE_URL + full_query.replace(" ", "+")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2500)

            # Dismiss consent / cookie popup if present
            await self._dismiss_consent(page)

            seen_names: set = set()
            count = 0

            stall_count = 0
            MAX_STALLS  = 3   # break after 3 scrolls with no new results

            while count < max_results:
                # Collect all visible listing anchors in the sidebar
                links = await page.query_selector_all('a[href*="/maps/place/"]')

                any_new = False
                for link in links:
                    if count >= max_results:
                        break

                    href = (await link.get_attribute("href") or "").strip()
                    place_id = href.split("/place/")[1].split("/")[0] if "/place/" in href else href
                    aria = (await link.get_attribute("aria-label") or "").strip()
                    key = place_id if place_id else aria
                    if not key or key in seen_names:
                        continue

                    seen_names.add(key)
                    any_new = True
                    stall_count = 0  # reset stall counter on new result

                    try:
                        await link.click()
                        await page.wait_for_timeout(self.delay_ms)
                        record = await self._extract_record(page)
                    except Exception:
                        # If a click fails, skip and continue with next listing
                        continue

                    if record.name:
                        yield record
                        count += 1
                        if progress_callback:
                            await progress_callback(count, max_results)

                if count >= max_results:
                    break

                if not any_new:
                    stall_count += 1
                    if stall_count >= MAX_STALLS:
                        # No new results after multiple scrolls — end of list
                        break

                # Scroll the results panel to trigger lazy-loading of more results
                scrolled = await self._scroll_panel(page)
                if not scrolled:
                    break

                await page.wait_for_timeout(1500)

        finally:
            await page.close()

    # ── Private helpers ──────────────────────────────────────────────────────

    async def _extract_record(self, page: Page) -> BusinessRecord:
        """
        Extract the 4 core fields from the currently-open detail panel.
        Tries multiple selector fallbacks for each field.
        Email is left empty here; use EmailFinder to populate it later.
        """
        record = BusinessRecord()

        # Name ────────────────────────────────────────────────────────────────
        for sel in self._NAME_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        record.name = text
                        break
            except Exception:
                continue

        # Address ─────────────────────────────────────────────────────────────
        for sel in self._ADDRESS_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) > 4:
                        record.address = text
                        break
            except Exception:
                continue

        # Phone ───────────────────────────────────────────────────────────────
        for sel in self._PHONE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    # Try aria-label first (most reliable)
                    aria = await el.get_attribute("aria-label") or ""
                    text = (await el.inner_text()).strip()
                    phone = aria if aria else text
                    # Strip non-digit noise from aria-label prefix
                    phone = re.sub(r"^[^+\d]*", "", phone).strip()
                    if phone:
                        record.phone = phone
                        break
            except Exception:
                continue

        # Website ─────────────────────────────────────────────────────────────
        for sel in self._WEBSITE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    href = (await el.get_attribute("href") or "").strip()
                    if href.startswith("http"):
                        record.website = href
                        break
            except Exception:
                continue

        return record

    async def _dismiss_consent(self, page: Page) -> None:
        """Click through Google's cookie/consent banner if it appears."""
        consent_selectors = [
            'button[aria-label*="Accept all"]',
            'button[aria-label*="Reject all"]',
            'form[action*="consent"] button',
        ]
        for sel in consent_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    return
            except Exception:
                continue

    async def _scroll_panel(self, page: Page) -> bool:
        """
        Scroll the Maps results sidebar panel to the bottom to trigger lazy loading.
        Returns False if no scrollable panel is found.
        """
        panel_selectors = [
            '[role="feed"]',
            'div[aria-label*="Results for"]',
            'div.m6QErb',
        ]
        for sel in panel_selectors:
            try:
                panel = await page.query_selector(sel)
                if panel:
                    # Scroll to absolute bottom of the scroll container to load next results
                    await panel.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                    return True
            except Exception:
                continue
        return False
