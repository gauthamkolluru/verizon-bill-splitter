"""Download Verizon bill PDFs with Playwright and a persisted browser session."""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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
VIEW_PDF_SELECTORS = (
    'button:has-text("View PDF")',
    'a:has-text("View PDF")',
)

DOWNLOAD_SELECTORS = VIEW_PDF_SELECTORS + (
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

DOCUMENTS_UI_TIMEOUT_MS = 30_000
PDF_CAPTURE_TIMEOUT_MS = 60_000
_NON_BILL_HINTS = ("receipt", "notification", "payment confirmation")
_CHROME_LABELS = {
    "monthly bills",
    "categories",
    "all categories",
    "documents & receipts",
    "documents and receipts",
    "view documents",
}
_GENERIC_LABELS = {
    "view",
    "download",
    "open",
    "print",
    "details",
    "pdf",
    "menu",
    "home",
    "account",
}
_MONTH_NAME_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b20\d{2}\b")
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_SENSITIVE_NUMBER_RE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b|\b\d{7,}\b")


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


def _normalize_label(label: str) -> str:
    return " ".join(str(label).split()).strip()


def choose_monthly_category(labels: list[str]) -> str | None:
    """Pick Monthly Bills, or the first visible label that contains Monthly."""
    cleaned = []
    for label in labels:
        text = _normalize_label(label)
        if text:
            cleaned.append((text, text.lower()))
    for text, lowered in cleaned:
        if lowered == "monthly bills":
            return text
    for text, lowered in cleaned:
        if "monthly bills" in lowered:
            return text
    for text, lowered in cleaned:
        if "monthly" in lowered:
            return text
    return None


def _looks_like_bill_document(label: str) -> bool:
    text = _normalize_label(label)
    lowered = text.lower()
    if not lowered or lowered in _CHROME_LABELS or lowered in _GENERIC_LABELS:
        return False
    if any(hint in lowered for hint in _NON_BILL_HINTS):
        return False
    if "bill" in lowered or "statement" in lowered:
        return True
    if _MONTH_NAME_RE.search(lowered) and _YEAR_RE.search(lowered):
        return True
    return _NUMERIC_DATE_RE.search(lowered) is not None


def is_view_pdf_label(label: str) -> bool:
    return "view pdf" in _normalize_label(label).lower()


def has_view_pdf_control(labels: list[str]) -> bool:
    return any(is_view_pdf_label(label) for label in labels)


_CSV_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")


def parse_csv_bill_date(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    for fmt in _CSV_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return parse_bill_date_string(text)


def latest_bill_month(csv_text: str) -> date | None:
    """Return the newest bill_date in a charges CSV, or None when it has none."""
    if not csv_text.strip():
        return None
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or "bill_date" not in reader.fieldnames:
        return None
    latest: date | None = None
    for row in reader:
        parsed = parse_csv_bill_date(str(row.get("bill_date") or ""))
        if parsed is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def is_newer_bill_month(bill_date: date | None, latest_month: date | None) -> bool:
    """True when bill_date is a later calendar month than the CSV cutoff.

    A missing cutoff means every bill is new. A missing bill date is kept so
    an unreadable PDF is not discarded.
    """
    if latest_month is None or bill_date is None:
        return True
    return (bill_date.year, bill_date.month) > (latest_month.year, latest_month.month)


def choose_newest_bill_label(labels: list[str]) -> str | None:
    """Return the first bill-like label. Callers pass labels newest-first."""
    for label in labels:
        if _looks_like_bill_document(label):
            return _normalize_label(label)
    for label in labels:
        if is_view_pdf_label(label):
            return _normalize_label(label)
    return None


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    media = content_type.split(";", 1)[0].strip().lower()
    return media or None


def looks_like_pdf_bytes(data: bytes | None) -> bool:
    if not data:
        return False
    return data.lstrip().startswith(b"%PDF")


def is_pdf_response(
    content_type: str | None = None,
    url: str | None = None,
    body: bytes | None = None,
) -> bool:
    """True when a response is a PDF by body, content-type, or .pdf URL."""
    if body is not None:
        if looks_like_pdf_bytes(body):
            return True
        if body:
            return False
    if _media_type(content_type) == "application/pdf":
        return True
    media = _media_type(content_type)
    if media in (None, "application/octet-stream", "binary/octet-stream"):
        return urlparse(url or "").path.lower().endswith(".pdf")
    return False


def redact_control_text(text: str, limit: int = 60) -> str:
    collapsed = _SENSITIVE_NUMBER_RE.sub("[redacted]", _normalize_label(text))
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def summarize_visible_controls(
    labels: list[str],
    *,
    limit: int = 12,
    text_limit: int = 60,
) -> str:
    shown = []
    for label in labels:
        text = redact_control_text(label, text_limit)
        if text:
            shown.append(text)
        if len(shown) >= limit:
            break
    return ", ".join(shown) if shown else "(none)"


def safe_log_url(url: str) -> str:
    """Drop query strings so session tokens are not written to the log."""
    parsed = urlparse(url or "")
    return parsed._replace(query="", params="", fragment=parsed.fragment[:40]).geturl()


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


def _read_locator_labels(
    locator: Any,
    *,
    visible_only: bool,
    limit: int = 40,
) -> list[str]:
    labels: list[str] = []
    try:
        count = min(locator.count(), limit)
    except Exception:
        return labels
    for index in range(count):
        item = locator.nth(index)
        try:
            if visible_only and not item.is_visible():
                continue
            raw = item.inner_text(timeout=1_000) if visible_only else item.text_content(timeout=1_000)
        except Exception:
            continue
        text = _normalize_label(raw or "")
        if text:
            labels.append(text)
    return labels


def _visible_control_texts(page: Any, limit: int = 30) -> list[str]:
    labels: list[str] = []
    for role in ("link", "button"):
        labels.extend(
            _read_locator_labels(
                page.get_by_role(role),
                visible_only=True,
                limit=limit,
            )
        )
        if len(labels) >= limit:
            return labels[:limit]
    return labels


def _visible_document_labels(page: Any) -> list[str]:
    labels = _read_locator_labels(
        page.locator("[role='row'], table tbody tr, [role='listitem']"),
        visible_only=True,
    )
    if any(_looks_like_bill_document(label) for label in labels):
        return labels
    return labels + _visible_control_texts(page)


def _failure_message(page: Any, reason: str) -> str:
    try:
        url = safe_log_url(page.url)
    except Exception:
        url = "(unavailable)"
    try:
        labels = _visible_control_texts(page)
    except Exception:
        labels = []
    return f"{reason} url={url} controls={summarize_visible_controls(labels)}"


def _wait_for_documents_ui(page: Any) -> None:
    """Ready when a View PDF control is visible, including non-button elements."""
    deadline = time.monotonic() + (DOCUMENTS_UI_TIMEOUT_MS / 1000)
    while time.monotonic() < deadline:
        try:
            labels = _visible_control_texts(page, limit=40)
        except Exception:
            labels = []
        if has_view_pdf_control(labels):
            logger.info("Verizon documents page is ready")
            return
        page.wait_for_timeout(400)
    raise TimeoutError("View PDF controls were not visible")


def _open_bills_page(page: Any, bills_url: str) -> None:
    logger.info("Opening Verizon bills page: %s", bills_url)
    page.goto(bills_url, wait_until="domcontentloaded")
    if looks_like_login_url(page.url):
        return
    try:
        _wait_for_documents_ui(page)
    except Exception as exc:
        if looks_like_login_url(page.url):
            return
        raise VerizonFetchError(
            _failure_message(page, "Documents list did not appear.")
        ) from exc


def _click_label(page: Any, label: str, roles: tuple[str, ...]) -> None:
    pattern = re.compile(r"\s+".join(re.escape(part) for part in label.split()), re.I)
    for role in roles:
        loc = page.get_by_role(role, name=pattern)
        try:
            if loc.count() == 0:
                continue
            target = loc.first
            if not target.is_visible():
                continue
            target.click()
            return
        except Exception:
            continue
    try:
        page.get_by_text(pattern).first.click()
    except Exception as exc:
        raise VerizonFetchError(
            f"Could not click {redact_control_text(label)}"
        ) from exc


def _select_native_option(select: Any, choice: str) -> None:
    options = select.locator("option")
    for index in range(options.count()):
        option = options.nth(index)
        raw = option.text_content(timeout=1_000) or ""
        if _normalize_label(raw) != choice:
            continue
        value = option.get_attribute("value")
        if value:
            select.select_option(value=value)
            return
        select.select_option(label=raw.strip() or choice)
        return
    select.select_option(label=choice)


def _open_category_menu(page: Any) -> bool:
    trigger = page.locator(
        "[role='combobox'], button:has-text('Categor'), [aria-label*='Categor' i]"
    )
    try:
        count = min(trigger.count(), 5)
    except Exception:
        return False
    for index in range(count):
        item = trigger.nth(index)
        try:
            if not item.is_visible():
                continue
            item.click()
            return True
        except Exception:
            continue
    return False


def _choose_open_menu_category(page: Any) -> str | None:
    labels = _read_locator_labels(
        page.locator("[role='option'], [role='menuitem']"),
        visible_only=True,
    )
    return choose_monthly_category(labels)


def _select_monthly_category(page: Any) -> None:
    try:
        _select_monthly_category_control(page)
    except Exception:
        logger.info("Could not change bill category; using the current document list")


def _select_monthly_category_control(page: Any) -> None:
    selects = page.locator("select")
    try:
        select_count = selects.count()
    except Exception:
        select_count = 0
    for index in range(select_count):
        select = selects.nth(index)
        try:
            if not select.is_visible():
                continue
            labels = _read_locator_labels(select.locator("option"), visible_only=False)
        except Exception:
            continue
        choice = choose_monthly_category(labels)
        if choice is None:
            continue
        _select_native_option(select, choice)
        logger.info("Selected bill category: %s", redact_control_text(choice))
        return

    choice = _choose_open_menu_category(page)
    if choice is None:
        if not _open_category_menu(page):
            logger.info("No Monthly Bills category control found; using the current document list")
            return
        try:
            page.locator("[role='option'], [role='menuitem']").first.wait_for(
                state="visible",
                timeout=10_000,
            )
        except Exception:
            logger.info("Category menu did not open; using the current document list")
            return
        choice = _choose_open_menu_category(page)
        if choice is None:
            logger.info("Category menu had no Monthly option; using the current document list")
            return
    _click_label(page, choice, ("option", "menuitem", "link", "button"))
    logger.info("Selected bill category: %s", redact_control_text(choice))


def _wait_for_bill_list(page: Any) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if choose_newest_bill_label(_visible_document_labels(page)):
            return
        page.wait_for_timeout(300)


def _verizon_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "verizon.com" or host.endswith(".verizon.com")


def _safe_suggested_name(name: str | None) -> str | None:
    if not name:
        return None
    safe = Path(name).name.strip()
    if not safe or safe in {".", ".."} or len(safe) > 120:
        return None
    return safe


def _suggested_filename(content_disposition: str | None, url: str | None) -> str | None:
    header = content_disposition or ""
    name = None
    encoded = re.search(r"filename\*=(?:UTF-8''|utf-8'')?([^;]+)", header, re.I)
    if encoded:
        name = unquote(encoded.group(1).strip().strip('"'))
    if not name:
        plain = re.search(r'filename="?([^";]+)"?', header, re.I)
        if plain:
            name = plain.group(1).strip()
    if not name and url:
        name = Path(urlparse(url).path).name
    safe = _safe_suggested_name(name)
    if safe and safe.lower().endswith(".pdf"):
        return safe
    return None


class _CapturedPdf:
    def __init__(
        self,
        suggested_filename: str | None,
        *,
        download: Any = None,
        body: bytes | None = None,
    ) -> None:
        self.suggested_filename = suggested_filename
        self._download = download
        self._body = body

    def save(self, dest: Path) -> None:
        if self._download is not None:
            self._download.save_as(str(dest))
            return
        if not looks_like_pdf_bytes(self._body):
            raise VerizonFetchError("Captured file was not a PDF")
        dest.write_bytes(self._body or b"")


def _assert_saved_pdf(dest: Path) -> None:
    if looks_like_pdf_bytes(dest.read_bytes()):
        return
    dest.unlink(missing_ok=True)
    raise VerizonFetchError("Downloaded file was not a PDF")


def _take_pending_pdf(pending: list[Any], found: dict[str, Any]) -> None:
    if found.get("download") or found.get("body") or not pending:
        return
    response = pending.pop(0)
    try:
        url = response.url
        content_type = response.headers.get("content-type", "")
        body = response.body()
        disposition = response.headers.get("content-disposition")
    except Exception:
        logger.info("PDF response body was not readable")
        return
    if not is_pdf_response(content_type, url, body):
        return
    found["body"] = body
    found["filename"] = _suggested_filename(disposition, url)


def _capture_popup_pdf(page: Any, found: dict[str, Any]) -> None:
    if found.get("download") or found.get("body"):
        return
    try:
        pages = list(page.context.pages)
    except Exception:
        return
    for extra in pages:
        if extra == page:
            continue
        try:
            url = extra.url
        except Exception:
            continue
        if not url.startswith("https://") or not _verizon_host(url):
            continue
        if not is_pdf_response(None, url):
            continue
        try:
            api_response = page.context.request.get(url)
            content_type = api_response.headers.get("content-type", "")
            body = api_response.body()
            disposition = api_response.headers.get("content-disposition")
        except Exception:
            continue
        if not is_pdf_response(content_type, url, body):
            continue
        found["body"] = body
        found["filename"] = _suggested_filename(disposition, url)
        return


def _capture_pdf_from_action(page: Any, action: Any) -> _CapturedPdf:
    """Click, then keep the first download event or PDF response/popup."""
    found: dict[str, Any] = {}
    pending: list[Any] = []

    def on_download(download: Any) -> None:
        found.setdefault("download", download)

    def on_response(response: Any) -> None:
        if found.get("download") or found.get("body") or pending:
            return
        try:
            url = response.url
            content_type = response.headers.get("content-type", "")
        except Exception:
            return
        if not is_pdf_response(content_type, url):
            return
        pending.append(response)

    def on_page(new_page: Any) -> None:
        new_page.on("response", on_response)

    page.on("download", on_download)
    page.on("response", on_response)
    page.context.on("page", on_page)
    try:
        try:
            action()
        except VerizonFetchError:
            raise
        except Exception as exc:
            raise VerizonFetchError("Could not click the bill document") from exc
        deadline = time.monotonic() + (PDF_CAPTURE_TIMEOUT_MS / 1000)
        while time.monotonic() < deadline and "download" not in found and "body" not in found:
            _take_pending_pdf(pending, found)
            if "download" in found or "body" in found:
                break
            _capture_popup_pdf(page, found)
            if "download" in found or "body" in found:
                break
            page.wait_for_timeout(250)
        _take_pending_pdf(pending, found)
    finally:
        page.remove_listener("download", on_download)
        page.remove_listener("response", on_response)
        page.context.remove_listener("page", on_page)

    if "download" in found:
        download = found["download"]
        return _CapturedPdf(
            _safe_suggested_name(getattr(download, "suggested_filename", None)),
            download=download,
        )
    if "body" in found:
        return _CapturedPdf(found.get("filename"), body=found["body"])
    raise VerizonFetchError("Clicked a bill but no PDF download or PDF response arrived")


def _download_from_documents(page: Any) -> _CapturedPdf:
    choice = choose_newest_bill_label(_visible_document_labels(page))
    if choice is None:
        raise VerizonFetchError("No monthly bill document title was visible")
    logger.info("Opening newest bill document: %s", redact_control_text(choice))
    return _capture_pdf_from_action(
        page,
        lambda: _click_label(page, choice, ("link", "button", "row")),
    )


def _click_first_download(page: Any) -> _CapturedPdf:
    """Fall back to labeled download controls when the document title does not yield a PDF."""
    last_error: Exception | None = None
    for selector in DOWNLOAD_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            if not locator.is_visible(timeout=2_000):
                continue
            return _capture_pdf_from_action(page, lambda loc=locator: loc.click())
        except Exception as exc:  # noqa: BLE001 - try next selector
            last_error = exc
            logger.debug("Download selector %r failed: %s", selector, type(exc).__name__)
            continue
    raise VerizonFetchError(
        "Could not find a bill PDF download control on the Verizon bills page. "
        "Update DOWNLOAD_SELECTORS in verizon_fetch.py if the UI changed."
    ) from last_error


def _acquire_bill_pdf(page: Any) -> _CapturedPdf:
    _select_monthly_category(page)
    _wait_for_bill_list(page)
    try:
        return _download_from_documents(page)
    except VerizonFetchError:
        logger.info("Bill title did not yield a PDF; trying download button selectors")
    try:
        return _click_first_download(page)
    except VerizonFetchError as exc:
        raise VerizonFetchError(
            _failure_message(page, "Could not download a monthly bill PDF.")
        ) from exc


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


def _view_pdf_locator(page: Any) -> Any:
    name = re.compile(r"view pdf", re.I)
    buttons = page.get_by_role("button", name=name)
    try:
        if buttons.count() > 0:
            return buttons
    except Exception:
        logger.debug("Could not count View PDF buttons")
    return page.get_by_role("link", name=name)


def _bill_date_from_saved_pdf(path: Path) -> date | None:
    from verizon_bill_splitter.parser import parse_bill_pdf

    try:
        result = parse_bill_pdf(path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read bill date from %s: %s", path.name, type(exc).__name__)
        return parse_bill_date_string(path.name)
    if result.bill_date:
        parsed = parse_csv_bill_date(result.bill_date)
        if parsed is not None:
            return parsed
    return parse_bill_date_string(path.name)


def _collect_missing_bills(
    page: Any,
    input_dir: Path,
    bills_url: str,
    latest_month: date | None,
    limit: int | None = None,
) -> list[Path]:
    """Click View PDF controls newest-first and keep months after latest_month."""
    saved: list[Path] = []
    skipped = 0
    index = 0
    while True:
        if index > 0:
            _open_bills_page(page, bills_url)
        locator = _view_pdf_locator(page)
        try:
            count = locator.count()
        except Exception as exc:
            raise VerizonFetchError(
                _failure_message(page, "Could not list View PDF controls.")
            ) from exc
        if count == 0:
            if saved or index:
                break
            raise VerizonFetchError(
                _failure_message(page, "No View PDF controls were visible.")
            )
        if index >= count:
            break
        target = locator.nth(index)
        logger.info("Opening View PDF %d of %d", index + 1, count)
        try:
            captured = _capture_pdf_from_action(page, lambda button=target: button.click())
        except VerizonFetchError as exc:
            logger.warning("Skipping View PDF %d: %s", index + 1, exc)
            skipped += 1
            index += 1
            continue
        temp = input_dir / f".fetching-{index}.pdf"
        try:
            captured.save(temp)
            _assert_saved_pdf(temp)
            bill_date = _bill_date_from_saved_pdf(temp)
            if not is_newer_bill_month(bill_date, latest_month):
                logger.info(
                    "Stopping at bill month %s; CSV cutoff is %s",
                    bill_date.isoformat() if bill_date else "unknown",
                    latest_month.isoformat() if latest_month else "none",
                )
                break
            dest = resolve_download_dest(
                input_dir,
                bill_date=bill_date,
                suggested_filename=captured.suggested_filename,
            )
            if dest.exists():
                logger.info("Bill already downloaded: %s", dest.name)
                saved.append(dest)
            else:
                temp.replace(dest)
                logger.info("Downloaded bill to %s", dest.name)
                saved.append(dest)
        finally:
            if temp.exists():
                temp.unlink()
        if limit is not None and len(saved) >= limit:
            break
        index += 1
    if not saved and skipped:
        raise VerizonFetchError("No View PDF produced a bill PDF")
    return saved


def fetch_missing_bills(
    input_dir: Path,
    storage_state: Path | None = None,
    *,
    bills_url: str = VERIZON_BILLS_URL,
    force_login: bool = False,
    headless: bool = False,
    latest_month: date | None = None,
    limit: int | None = None,
) -> list[Path]:
    """Download Verizon bill PDFs newer than latest_month into input_dir."""
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
        _open_bills_page(page, bills_url)

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
            _open_bills_page(page, bills_url)
            if looks_like_login_url(page.url):
                browser.close()
                raise VerizonFetchError(
                    "Verizon session still invalid after login. Try: python parse_bill.py --login"
                )

        saved = _collect_missing_bills(
            page, input_dir, bills_url, latest_month, limit=limit
        )
        _save_storage_state(context, storage_state)
        browser.close()
        return saved


def fetch_latest_bill(
    input_dir: Path,
    storage_state: Path | None = None,
    *,
    bills_url: str = VERIZON_BILLS_URL,
    force_login: bool = False,
    headless: bool = False,
) -> Path | None:
    """Download only the newest Verizon bill PDF into input_dir."""
    saved = fetch_missing_bills(
        input_dir,
        storage_state,
        bills_url=bills_url,
        force_login=force_login,
        headless=headless,
        latest_month=None,
        limit=1,
    )
    return saved[0] if saved else None
