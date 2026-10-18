from datetime import date
from pathlib import Path

import pytest

from verizon_bill_splitter.verizon_fetch import (
    bill_pdf_filename,
    looks_like_login_url,
    parse_bill_date_string,
    resolve_download_dest,
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
