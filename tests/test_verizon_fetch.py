from datetime import date
from pathlib import Path

import pytest

from verizon_bill_splitter.verizon_fetch import (
    bill_pdf_filename,
    choose_monthly_category,
    choose_newest_bill_label,
    has_view_pdf_control,
    is_newer_bill_month,
    is_pdf_response,
    latest_bill_month,
    looks_like_login_url,
    parse_bill_date_string,
    resolve_download_dest,
    safe_log_url,
    summarize_visible_controls,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://login.verizon.com/signin", True),
        ("https://www.verizon.com/login/", True),
        ("https://www.verizon.com/digital/nsa/secure/ui/bill/docs/#/", False),
        ("https://www.verizon.com/digital/#/login", True),
        ("https://id.verizon.com/auth/oauth", True),
        ("", False),
    ],
)
def test_looks_like_login_url(url: str, expected: bool):
    assert looks_like_login_url(url) is expected


def test_bill_pdf_filename_from_date():
    assert bill_pdf_filename(date(2026, 3, 15)) == "MyBill_03.15.2026.pdf"


def test_bill_pdf_filename_from_string():
    assert bill_pdf_filename("03/15/2026") == "MyBill_03.15.2026.pdf"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bill date: 03/15/2026", date(2026, 3, 15)),
        ("MyBill_03.15.2026.pdf", date(2026, 3, 15)),
        ("statement 3-5-26", date(2026, 3, 5)),
        ("no date here", None),
    ],
)
def test_parse_bill_date_string(text: str, expected: date | None):
    assert parse_bill_date_string(text) == expected


def test_resolve_download_dest_prefers_bill_date(tmp_path: Path):
    dest = resolve_download_dest(tmp_path, bill_date=date(2026, 1, 2))
    assert dest == tmp_path / "MyBill_01.02.2026.pdf"


def test_resolve_download_dest_from_suggested_filename(tmp_path: Path):
    dest = resolve_download_dest(
        tmp_path,
        suggested_filename="Verizon_Bill_04.01.2026.pdf",
    )
    assert dest == tmp_path / "MyBill_04.01.2026.pdf"


def test_resolve_download_dest_keeps_plain_pdf_name(tmp_path: Path):
    dest = resolve_download_dest(tmp_path, suggested_filename="statement.pdf")
    assert dest == tmp_path / "statement.pdf"


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["Receipts", "Monthly Bills", "Notifications"], "Monthly Bills"),
        (["  monthly bills  "], "monthly bills"),
        (["monthly", "Monthly Bills"], "Monthly Bills"),
        (["Something Monthly Bills extra", "Monthly other"], "Something Monthly Bills extra"),
        (["Receipts", "Monthly statements"], "Monthly statements"),
        (["Notifications", "Receipts"], None),
        (["", "   "], None),
        ([], None),
    ],
)
def test_choose_monthly_category(labels: list[str], expected: str | None):
    assert choose_monthly_category(labels) == expected


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (
            ["Payment Receipt", "Account notification", "March 2026 Bill", "January 2026 Bill"],
            "March 2026 Bill",
        ),
        (["Receipt", "Notification"], None),
        (["Payment confirmation", "Wireless statement"], "Wireless statement"),
        (["Oct 17, 2026", "Sep 17, 2026"], "Oct 17, 2026"),
        (["03/15/2026"], "03/15/2026"),
        (["Download", "View", "My Bill"], "My Bill"),
        (["Monthly Bills", "October 2026 statement"], "October 2026 statement"),
        (["Categories", "View"], None),
        (["May be later"], None),
        (["  March   2026 Bill  "], "March 2026 Bill"),
        (["Sign out", "View PDF", "View PDF", "View PDF"], "View PDF"),
        (["View PDF", "March 2026 Bill"], "March 2026 Bill"),
        ([], None),
    ],
)
def test_choose_newest_bill_label(labels: list[str], expected: str | None):
    assert choose_newest_bill_label(labels) == expected


def test_has_view_pdf_control_matches_logged_page():
    labels = [
        "Personal",
        "Business",
        "Sign out",
        "View PDF",
        "View PDF",
        "View PDF",
    ]
    assert has_view_pdf_control(labels) is True
    assert has_view_pdf_control(["Personal", "Sign out"]) is False


def test_latest_bill_month_uses_newest_date():
    csv_text = "bill_date,amount\n03/15/2026,1\n01/15/2026,2\n10/02/2025,3\n"
    assert latest_bill_month(csv_text) == date(2026, 3, 15)


def test_latest_bill_month_empty():
    assert latest_bill_month("") is None
    assert latest_bill_month("amount\n1\n") is None


@pytest.mark.parametrize(
    ("bill_date", "latest", "expected"),
    [
        (date(2026, 4, 2), date(2026, 3, 15), True),
        (date(2026, 3, 20), date(2026, 3, 15), False),
        (date(2026, 2, 1), date(2026, 3, 15), False),
        (date(2027, 1, 1), date(2026, 12, 1), True),
        (None, date(2026, 3, 15), True),
        (date(2026, 4, 1), None, True),
    ],
)
def test_is_newer_bill_month(bill_date: date | None, latest: date | None, expected: bool):
    assert is_newer_bill_month(bill_date, latest) is expected


@pytest.mark.parametrize(
    ("content_type", "url", "body", "expected"),
    [
        ("application/pdf", None, None, True),
        ("application/pdf; charset=binary", None, None, True),
        ("APPLICATION/PDF", None, None, True),
        ("text/html", None, None, False),
        ("text/html", "https://www.verizon.com/bill.pdf", None, False),
        ("application/octet-stream", "https://www.verizon.com/doc/bill.pdf", None, True),
        (None, "https://www.verizon.com/doc/bill.pdf", None, True),
        (None, "https://www.verizon.com/docs", None, False),
        (None, None, b"%PDF-1.7\n", True),
        (None, None, b"  %PDF-1.4", True),
        ("application/pdf", None, b"<html>not a pdf</html>", False),
        (None, None, b"", False),
        (None, None, None, False),
    ],
)
def test_is_pdf_response(content_type, url, body, expected: bool):
    assert is_pdf_response(content_type, url, body) is expected


def test_summarize_visible_controls_redacts_account_and_phone():
    summary = summarize_visible_controls(
        ["Account 12345678901 Bill", "Call 555-123-4567", "March 2026 Bill"]
    )
    assert "12345678901" not in summary
    assert "555-123-4567" not in summary
    assert "[redacted]" in summary
    assert "March 2026 Bill" in summary


def test_summarize_visible_controls_truncates_and_caps_count():
    labels = [f"Bill {index}" for index in range(20)]
    labels.append("x" * 200)
    summary = summarize_visible_controls(labels)
    assert summary.count("Bill ") == 12
    assert "Bill 12" not in summary
    long_only = summarize_visible_controls(["y" * 200])
    assert len(long_only) < 80
    assert long_only.endswith("…")


def test_summarize_visible_controls_empty():
    assert summarize_visible_controls([]) == "(none)"
    assert summarize_visible_controls(["  ", ""]) == "(none)"


def test_safe_log_url_strips_query():
    url = "https://www.verizon.com/digital/nsa/secure/ui/bill/docs/?token=secret#/monthly"
    logged = safe_log_url(url)
    assert "secret" not in logged
    assert "token" not in logged
    assert logged.startswith("https://www.verizon.com/digital/nsa/secure/ui/bill/docs/")
