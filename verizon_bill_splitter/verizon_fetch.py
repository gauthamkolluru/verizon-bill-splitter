"""Download Verizon bill PDFs with Playwright and a persisted browser session."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("verizon_bill_splitter.verizon_fetch")

VERIZON_BILLS_URL = "https://www.verizon.com/digital/nsa/secure/ui/bill/docs/#/"
DEFAULT_STORAGE_STATE = Path("config/verizon_storage_state.json")

LOGIN_HOST_HINTS = (
    "login.verizon.com",
    "signin.verizon.com",
    "id.verizon.com",
    "auth.verizon.com",
)
LOGIN_PATH_HINTS = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth",
    "/sso",
    "/identify",
)

# Tried in order; Verizon's SPA labels change — update here when downloads break.
DOWNLOAD_SELECTORS = (
    'a:has-text("Review bill PDF")',
    'button:has-text("Review bill PDF")',
    'a:has-text("Download PDF")',
    'button:has-text("Download PDF")',
    'a:has-text("Download bill")',
    'button:has-text("Download bill")',
    'a[href*=".pdf"]',
    'a[download*=".pdf"]',
)

BILL_DATE_PATTERNS = (
    re.compile(
        r"bill\s*date[:\s]+(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*(?:bill|statement)",
        re.IGNORECASE,
    ),
    re.compile(r"MyBill_(\d{2})\.(\d{2})\.(\d{4})", re.IGNORECASE),
)

SUGGESTED_DATE_RE = re.compile(
    r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})"
)


class VerizonFetchError(RuntimeError):
    """Raised when the bill cannot be downloaded."""


def default_storage_state_path() -> Path:
    return DEFAULT_STORAGE_STATE


def looks_like_login_url(url: str) -> bool:
    """Return True when the URL likely means the session is not authenticated."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if any(hint in host for hint in LOGIN_HOST_HINTS):
        return True
    if any(hint in path for hint in LOGIN_PATH_HINTS):
        return True
    # Verizon sometimes parks unauthenticated users on a login hash route.
    fragment = (parsed.fragment or "").lower()
    if "login" in fragment or "signin" in fragment or "sign-in" in fragment:
        return True
    return False


def bill_pdf_filename(bill_date: date | datetime | str) -> str:
    """Build MyBill_MM.DD.YYYY.pdf to match parser filename fallback."""
    if isinstance(bill_date, datetime):
        d = bill_date.date()
    elif isinstance(bill_date, date):
        d = bill_date
    else:
        parsed = parse_bill_date_string(str(bill_date))
        if parsed is None:
            raise ValueError(f"Unrecognized bill date: {bill_date!r}")
        d = parsed
    return f"MyBill_{d.month:02d}.{d.day:02d}.{d.year:04d}.pdf"


def parse_bill_date_string(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    for pattern in BILL_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            month, day, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if year < 100:
                year += 2000
            try:
                return date(year, month, day)
            except ValueError:
                continue
    match = SUGGESTED_DATE_RE.search(text)
    if match:
        month, day, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def resolve_download_dest(
    input_dir: Path,
    *,
    bill_date: date | None = None,
    suggested_filename: str | None = None,
) -> Path:
    """Choose a destination path under input_dir for the downloaded PDF."""
    input_dir.mkdir(parents=True, exist_ok=True)
    if bill_date is not None:
        return input_dir / bill_pdf_filename(bill_date)
    if suggested_filename:
        parsed = parse_bill_date_string(suggested_filename)
        if parsed is not None:
            return input_dir / bill_pdf_filename(parsed)
        name = Path(suggested_filename).name
        if name.lower().endswith(".pdf"):
            return input_dir / name
    today = date.today()
    return input_dir / bill_pdf_filename(today)


def _wait_for_manual_login(page: Any) -> None:
    print(
        "Verizon login required. Complete sign-in (including MFA) in the browser window,\n"
        "then press Enter here to continue...",
        flush=True,
    )
    try:
        input()
    except EOFError:
        # Non-interactive terminals: wait until the URL leaves the login surface.
        logger.info("No stdin available; waiting for login URL to clear")
        page.wait_for_function(
            """() => {
                const href = location.href.toLowerCase();
                return !(
                    href.includes('login') ||
                    href.includes('signin') ||
                    href.includes('sign-in') ||
                    href.includes('/auth')
                );
            }""",
            timeout=600_000,
        )


def _save_storage_state(context: Any, storage_state: Path) -> None:
    storage_state.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_state))
    logger.info("Saved Verizon browser session to %s", storage_state)


def _page_bill_date(page: Any) -> date | None:
    try:
        text = page.inner_text("body", timeout=5_000)
    except Exception:
        return None
    return parse_bill_date_string(text)


def _click_first_download(page: Any) -> Any:
    """Click the first matching download control and return the Download object."""
    last_error: Exception | None = None
    for selector in DOWNLOAD_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            if not locator.is_visible(timeout=2_000):
                continue
            with page.expect_download(timeout=60_000) as download_info:
                locator.click()
            return download_info.value
        except Exception as exc:  # noqa: BLE001 - try next selector
            last_error = exc
            logger.debug("Download selector %r failed: %s", selector, exc)
            continue
    raise VerizonFetchError(
        "Could not find a bill PDF download control on the Verizon bills page. "
        "Update DOWNLOAD_SELECTORS in verizon_fetch.py if the UI changed."
    ) from last_error


def login_and_save_session(
    storage_state: Path | None = None,
    *,
    bills_url: str = VERIZON_BILLS_URL,
) -> Path:
    """Open a headed browser for manual login and persist storage_state."""
    storage_state = storage_state or default_storage_state_path()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise VerizonFetchError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        logger.info("Opening Verizon bills page for login: %s", bills_url)
        page.goto(bills_url, wait_until="domcontentloaded")
        _wait_for_manual_login(page)
        if looks_like_login_url(page.url):
            # User pressed Enter early; give one more chance after a short settle.
            page.wait_for_timeout(2_000)
        if looks_like_login_url(page.url):
            browser.close()
            raise VerizonFetchError(
                "Still on a login page after manual login. Re-run with --login and finish sign-in."
            )
        _save_storage_state(context, storage_state)
        browser.close()
    return storage_state


def fetch_latest_bill(
    input_dir: Path,
    storage_state: Path | None = None,
    *,
    bills_url: str = VERIZON_BILLS_URL,
    force_login: bool = False,
    headless: bool = False,
) -> Path | None:
    """
    Download the latest Verizon bill PDF into input_dir.

    Returns the destination path, or None if a matching PDF is already present
    and the page date could be resolved to that same filename before download.
    """
    storage_state = storage_state or default_storage_state_path()
    input_dir = Path(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise VerizonFetchError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    if force_login or not storage_state.exists():
        logger.info("Verizon session missing or re-login requested")
        login_and_save_session(storage_state, bills_url=bills_url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            accept_downloads=True,
            storage_state=str(storage_state),
        )
        page = context.new_page()
        logger.info("Opening Verizon bills page: %s", bills_url)
        page.goto(bills_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)

        if looks_like_login_url(page.url):
            logger.info("Saved session expired; requesting manual login")
            browser.close()
            login_and_save_session(storage_state, bills_url=bills_url)
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(
                accept_downloads=True,
                storage_state=str(storage_state),
            )
            page = context.new_page()
            page.goto(bills_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2_000)
            if looks_like_login_url(page.url):
                browser.close()
                raise VerizonFetchError(
                    "Verizon session still invalid after login. Try: python parse_bill.py --login"
                )

        bill_date = _page_bill_date(page)
        if bill_date is not None:
            dest = resolve_download_dest(input_dir, bill_date=bill_date)
            if dest.exists():
                logger.info("Bill already downloaded: %s", dest.name)
                browser.close()
                return dest

        download = _click_first_download(page)
        suggested = download.suggested_filename
        if bill_date is None:
            bill_date = parse_bill_date_string(suggested or "")
        dest = resolve_download_dest(
            input_dir,
            bill_date=bill_date,
            suggested_filename=suggested,
        )
        if dest.exists():
            logger.info("Bill already downloaded: %s", dest.name)
            browser.close()
            return dest

        download.save_as(str(dest))
        _save_storage_state(context, storage_state)
        browser.close()
        logger.info("Downloaded bill to %s", dest)
        return dest
