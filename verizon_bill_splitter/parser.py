from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pdfplumber
import yaml

logger = logging.getLogger("verizon_bill_splitter.parser")

SUMMARY_HEADER = "bill summary by line"
ACCOUNT_WIDE_PHRASE = "account-wide charges"
SKIP_PHRASES = (
    "questions about your bill",
    "review your bill online",
    "surcharges, taxes and gov fees",
    "scan the qr code",
    "an itemized bill breakdown",
)

DESCRIPTION_X_MAX = 280
LINE_AMOUNT_X_MIN = 280
LINE_AMOUNT_X_MAX = 335

PHONE_RE = re.compile(
    r"\(?(?:\s*(\d{3})\s*)\)?\s*[-.\s]?\s*(\d{3})\s*[-.\s]?\s*(\d{4})\)?"
)
AMOUNT_RE = re.compile(r"^-?\$[\d,]+\.\d{2}$")
NAME_RE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$")
BILLING_PERIOD_RE = re.compile(
    r"(?:quick bill summary\s+|billing period:\s*)?"
    r"([A-Za-z]+\s+\d{1,2})\s*[-–]\s*([A-Za-z]+\s+\d{1,2})",
    re.IGNORECASE,
)
ACCOUNT_RE = re.compile(r"Account:\s*(\S+)", re.IGNORECASE)
INVOICE_RE = re.compile(r"Invoice(?:\s+number)?[:\s]+(\S+)", re.IGNORECASE)
TOTAL_DUE_RE = re.compile(r"total\s+amount\s+due[^\d$]*(-?\$[\d,]+\.\d{2})", re.IGNORECASE)
BILL_TOTAL_RE = re.compile(r"Your\s+\w+\s+bill\s+is\s+(-?\$[\d,]+\.\d{2})", re.IGNORECASE)
TOTAL_CURRENT_RE = re.compile(
    r"total\s+current\s+charges[^\d$]*(-?\$[\d,]+\.\d{2})", re.IGNORECASE
)
STATUS_PHRASES = (
    "device upgraded",
    "perk added",
    "perk removed",
    "new plan added",
    "new device added",
    "plan changed",
    "service added",
    "service removed",
)

CHARGE_COLUMNS = [
    "bill_date",
    "billing_period_start",
    "billing_period_end",
    "account_number",
    "invoice_number",
    "total_amount_due",
    "total_current_charges",
    "source_file",
    "charge_type",
    "user",
    "phone",
    "name_on_bill",
    "device_or_plan",
    "description",
    "amount",
]

EXPORT_COLUMNS = [
    "bill_date",
    "billing_period",
    "phone",
    "device",
    "charge_type",
    "amount",
    "bill_total",
    "paid",
]

PAID_COLUMN = "paid"

AGGREGATED_CSV_NAME = "charges.csv"


@dataclass
class LineCharge:
    phone: str | None
    name: str | None
    device_or_plan: str | None
    description: str
    amount: float | None
    charge_type: str = "line_item"


@dataclass
class BillParseResult:
    source_file: Path
    bill_date: str | None
    account_number: str | None
    invoice_number: str | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    total_amount_due: float | None = None
    total_current_charges: float | None = None
    lines: list[LineCharge] = field(default_factory=list)

    def to_dataframe(self, user_map: dict[str, str] | None = None) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for line in self.lines:
            normalized_phone = normalize_phone(line.phone) if line.phone else None
            user = None
            if user_map and normalized_phone:
                user = user_map.get(normalized_phone)
            rows.append(
                {
                    "bill_date": self.bill_date,
                    "billing_period_start": self.billing_period_start,
                    "billing_period_end": self.billing_period_end,
                    "account_number": self.account_number,
                    "invoice_number": self.invoice_number,
                    "total_amount_due": self.total_amount_due,
                    "total_current_charges": self.total_current_charges,
                    "source_file": self.source_file.name,
                    "charge_type": line.charge_type,
                    "user": user or line.name,
                    "phone": normalized_phone or line.phone,
                    "name_on_bill": line.name,
                    "device_or_plan": line.device_or_plan,
                    "description": line.description,
                    "amount": line.amount,
                }
            )
        if not rows:
            return pd.DataFrame(columns=CHARGE_COLUMNS)
        return pd.DataFrame(rows)[CHARGE_COLUMNS]


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    collapsed = re.sub(r"\s+", "", value)
    for candidate in (collapsed, value):
        match = PHONE_RE.search(candidate)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def parse_amount(value: str) -> float:
    return float(value.replace("$", "").replace(",", ""))


def load_user_map(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    lines = data.get("lines", {})
    return {normalize_phone(str(phone)) or str(phone): str(user) for phone, user in lines.items()}


def extract_metadata_from_text(text: str) -> dict[str, Any]:
    bill_date = None
    account_number = None
    invoice_number = None
    billing_period_start = None
    billing_period_end = None
    total_amount_due = None
    total_current_charges = None

    account_match = ACCOUNT_RE.search(text)
    if account_match:
        account_number = account_match.group(1)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if lower.startswith("bill date"):
            bill_date = line.split("Bill date", 1)[-1].strip(" :\t")
        elif lower.startswith("account number") and not account_number:
            account_number = line.split("Account number", 1)[-1].strip(" :\t")

    invoice_match = INVOICE_RE.search(text)
    if invoice_match:
        invoice_number = invoice_match.group(1)

    period_match = BILLING_PERIOD_RE.search(text)
    if period_match:
        billing_period_start = period_match.group(1).strip()
        billing_period_end = period_match.group(2).strip()

    total_due_match = BILL_TOTAL_RE.search(text) or TOTAL_DUE_RE.search(text)
    if total_due_match:
        total_amount_due = parse_amount(total_due_match.group(1))

    total_current_match = TOTAL_CURRENT_RE.search(text)
    if total_current_match:
        total_current_charges = parse_amount(total_current_match.group(1))

    return {
        "bill_date": bill_date,
        "account_number": account_number,
        "invoice_number": invoice_number,
        "billing_period_start": billing_period_start,
        "billing_period_end": billing_period_end,
        "total_amount_due": total_amount_due,
        "total_current_charges": total_current_charges,
    }


def parse_bill_pdf(pdf_path: str | Path) -> BillParseResult:
    pdf_path = Path(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        metadata_text = "\n".join(
            (page.extract_text() or "") for page in pdf.pages[:3]
        )
        metadata = extract_metadata_from_text(metadata_text)
        summary_page = _find_summary_page(pdf)
        if summary_page is None:
            raise ValueError(
                f'Could not find "{SUMMARY_HEADER}" section in {pdf_path.name}. '
                "If Verizon changed the bill layout, update parser.py."
            )
        lines = _extract_line_charges(summary_page)

    if not metadata.get("bill_date"):
        metadata.update(_bill_date_from_filename(pdf_path))

    return BillParseResult(
        source_file=pdf_path,
        bill_date=metadata.get("bill_date"),
        account_number=metadata.get("account_number"),
        invoice_number=metadata.get("invoice_number"),
        billing_period_start=metadata.get("billing_period_start"),
        billing_period_end=metadata.get("billing_period_end"),
        total_amount_due=metadata.get("total_amount_due"),
        total_current_charges=metadata.get("total_current_charges"),
        lines=lines,
    )


def _bill_date_from_filename(pdf_path: Path) -> dict[str, str | None]:
    """Fallback: MyBill_MM.DD.YYYY.pdf → bill date."""
    match = re.match(r"MyBill_(\d{2})\.(\d{2})\.(\d{4})", pdf_path.stem, re.IGNORECASE)
    if not match:
        return {"bill_date": None}
    month, day, year = match.groups()
    return {"bill_date": f"{month}/{day}/{year}"}


def _find_summary_page(pdf: pdfplumber.PDF) -> pdfplumber.page.Page | None:
    for page in pdf.pages:
        text = (page.extract_text() or "").lower()
        if SUMMARY_HEADER in text:
            return page
    return None


def _extract_line_charges(page: pdfplumber.page.Page) -> list[LineCharge]:
    words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
    rows = _group_words_into_rows(words)
    rows = sorted(rows, key=lambda row: row[0]["top"])
    return _parse_summary_coordinate_rows(rows)


def _left_column_words(row: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [word for word in row if word["x0"] < DESCRIPTION_X_MAX]


def _line_amount_word(row: list[dict[str, Any]]) -> dict[str, Any] | None:
    for word in row:
        if (
            AMOUNT_RE.match(word["text"])
            and LINE_AMOUNT_X_MIN <= word["x0"] <= LINE_AMOUNT_X_MAX
        ):
            return word
    return None


def _is_status_row(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return any(phrase in normalized for phrase in STATUS_PHRASES)


def _is_device_row(text: str) -> bool:
    normalized = text.lower()
    return bool(normalize_phone(text)) or any(
        token in normalized for token in ("iphone", "ipad", "galaxy", "pixel", "watch")
    )


def _find_device_row(
    rows: list[list[dict[str, Any]]], name_row_index: int
) -> str | None:
    name_top = rows[name_row_index][0]["top"]
    for row in rows[name_row_index + 1 : name_row_index + 4]:
        top = row[0]["top"]
        if top - name_top > 30:
            break
        if top - name_top < 8:
            continue
        text = _row_text(_left_column_words(row))
        if not text or _is_status_row(text):
            continue
        if _is_device_row(text):
            return text
    return None


def _parse_summary_coordinate_rows(rows: list[list[dict[str, Any]]]) -> list[LineCharge]:
    in_section = False
    lines: list[LineCharge] = []

    for index, row in enumerate(rows):
        text = _row_text(row)
        if not text:
            continue

        normalized = " ".join(text.lower().split())
        if SUMMARY_HEADER in normalized:
            in_section = True
            continue
        if not in_section:
            continue

        amount_word = _line_amount_word(row)
        if amount_word is None:
            continue

        left_text = _row_text(_left_column_words(row))
        if _is_account_wide(left_text or text):
            lines.append(
                LineCharge(
                    phone=None,
                    name=None,
                    device_or_plan=None,
                    description="Account-wide charges & credits",
                    amount=parse_amount(amount_word["text"]),
                    charge_type="account_wide",
                )
            )
            continue

        if not left_text or _should_skip(left_text) or _is_status_row(left_text):
            continue

        name = _extract_name(left_text) or left_text.strip()
        device_or_plan = _find_device_row(rows, index)
        phone = normalize_phone(device_or_plan) if device_or_plan else None
        description = left_text if not device_or_plan else f"{left_text} | {device_or_plan}"

        lines.append(
            LineCharge(
                phone=phone,
                name=name,
                device_or_plan=device_or_plan,
                description=description,
                amount=parse_amount(amount_word["text"]),
                charge_type="line_item",
            )
        )

    return lines


def _group_words_into_rows(words: list[dict[str, Any]], y_tolerance: float = 4.0) -> list[list[dict[str, Any]]]:
    if not words:
        return []

    sorted_words = sorted(words, key=lambda w: (-w["top"], w["x0"]))
    rows: list[list[dict[str, Any]]] = []
    current_row: list[dict[str, Any]] = [sorted_words[0]]
    current_top = sorted_words[0]["top"]

    for word in sorted_words[1:]:
        if abs(word["top"] - current_top) <= y_tolerance:
            current_row.append(word)
        else:
            rows.append(sorted(current_row, key=lambda w: w["x0"]))
            current_row = [word]
            current_top = word["top"]
    rows.append(sorted(current_row, key=lambda w: w["x0"]))
    return rows


def _row_text(row: list[dict[str, Any]]) -> str:
    return " ".join(word["text"] for word in row).strip()


def _is_account_wide(text: str) -> bool:
    return ACCOUNT_WIDE_PHRASE in " ".join(text.lower().split())


def _should_skip(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    if not normalized:
        return True
    if normalized in {"total:", "total"} or normalized.startswith("total:"):
        return True
    if _is_account_wide(normalized):
        return False
    return any(phrase in normalized for phrase in SKIP_PHRASES)


def _split_line_parts(text: str) -> tuple[str | None, str | None, str]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) >= 2 and NAME_RE.match(lines[0]):
        return lines[0], " ".join(lines[1:]), text
    name = _extract_name(text)
    if name:
        remainder = text.replace(name, "", 1).strip()
        return name, remainder or None, text
    return None, None, text


def _extract_name(description: str) -> str | None:
    first_line = description.split("\n", 1)[0].strip()
    if NAME_RE.match(first_line):
        return first_line
    tokens = first_line.split()
    if len(tokens) >= 2:
        name_prefix = " ".join(tokens[:2])
        if not any(char.isdigit() for char in name_prefix):
            if len(tokens) == 2 and tokens[0][0].isupper() and tokens[1][0].isupper():
                return first_line
            if len(tokens) > 2 and tokens[0][0].isupper() and tokens[1][0].isupper():
                return name_prefix
        if not any(char.isdigit() for char in first_line):
            candidate = " ".join(tokens[: min(len(tokens), 4)])
            if NAME_RE.match(candidate):
                return candidate
    return None


def aggregated_csv_path(output_dir: Path) -> Path:
    return output_dir / AGGREGATED_CSV_NAME


def _format_billing_period(start: str | None, end: str | None) -> str | None:
    if start and end:
        return f"{start} - {end}"
    return start or end


def _format_charge_type(charge_type: str | None) -> str | None:
    if charge_type == "account_wide":
        return "Account"
    if charge_type == "line_item":
        return "Line"
    return charge_type


def _export_row_key(row: pd.Series) -> tuple[str, str, str]:
    phone = row.get("phone")
    if phone is None or (isinstance(phone, float) and pd.isna(phone)):
        phone = ""
    return (
        str(row.get("bill_date", "")),
        str(phone),
        str(row.get("charge_type", "")),
    )


def _load_paid_map(csv_path: Path) -> dict[tuple[str, str, str], str]:
    if not csv_path.exists():
        return {}
    try:
        existing = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    except (OSError, pd.errors.EmptyDataError, ValueError) as exc:
        logger.warning("Could not read paid values from %s: %s", csv_path, exc)
        return {}
    if PAID_COLUMN not in existing.columns:
        return {}

    paid_map: dict[tuple[str, str, str], str] = {}
    for _, row in existing.iterrows():
        paid = str(row.get(PAID_COLUMN, "")).strip()
        if paid:
            paid_map[_export_row_key(row)] = paid
    return paid_map


def _apply_paid(export: pd.DataFrame, paid_map: dict[tuple[str, str, str], str]) -> pd.DataFrame:
    if export.empty:
        return pd.DataFrame(columns=EXPORT_COLUMNS)
    export = export.copy()
    export[PAID_COLUMN] = export.apply(
        lambda row: paid_map.get(_export_row_key(row), ""),
        axis=1,
    )
    return export[EXPORT_COLUMNS]


def prepare_export_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=EXPORT_COLUMNS)

    export = pd.DataFrame(
        {
            "bill_date": df["bill_date"],
            "billing_period": [
                _format_billing_period(start, end)
                for start, end in zip(
                    df.get("billing_period_start", pd.Series(dtype=object)),
                    df.get("billing_period_end", pd.Series(dtype=object)),
                    strict=False,
                )
            ],
            "phone": df["phone"],
            "device": df["device_or_plan"],
            "charge_type": df["charge_type"].map(_format_charge_type),
            "amount": df["amount"],
            "bill_total": df["total_amount_due"],
        }
    )
    return _sort_charges(export)


def _sort_charges(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    sort_df = df.copy()
    if "bill_date" in sort_df.columns:
        sort_df["_bill_date_sort"] = pd.to_datetime(sort_df["bill_date"], errors="coerce")
    sort_cols = [c for c in ("_bill_date_sort", "phone", "charge_type") if c in sort_df.columns]
    ascending = [False] + [True] * (len(sort_cols) - 1)
    sorted_df = sort_df.sort_values(sort_cols, ascending=ascending, na_position="last").drop(
        columns=["_bill_date_sort"], errors="ignore"
    )
    export_cols = [c for c in EXPORT_COLUMNS if c in sorted_df.columns]
    return sorted_df.reset_index(drop=True)[export_cols]


def _read_export_csv(text: str) -> pd.DataFrame:
    if not text or not text.strip():
        return pd.DataFrame(columns=EXPORT_COLUMNS)
    frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    for column in EXPORT_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    return frame[EXPORT_COLUMNS]


def merge_charge_csv_texts(local_text: str, remote_text: str | None) -> str:
    """Union local and Drive rows. Drive-only rows and blank-local paid values stay."""
    local = _read_export_csv(local_text)
    if not remote_text or not remote_text.strip():
        return local.to_csv(index=False)

    remote = _read_export_csv(remote_text)
    remote_by_key = {_export_row_key(row): row for _, row in remote.iterrows()}
    local_keys: set[tuple[str, str, str]] = set()
    rows: list[pd.Series] = []
    for _, row in local.iterrows():
        key = _export_row_key(row)
        local_keys.add(key)
        merged = row.copy()
        remote_row = remote_by_key.get(key)
        if remote_row is not None:
            local_paid = str(merged.get(PAID_COLUMN, "")).strip()
            remote_paid = str(remote_row.get(PAID_COLUMN, "")).strip()
            if not local_paid and remote_paid:
                merged[PAID_COLUMN] = remote_paid
        rows.append(merged)
    for key, row in remote_by_key.items():
        if key not in local_keys:
            rows.append(row)
    if not rows:
        return pd.DataFrame(columns=EXPORT_COLUMNS).to_csv(index=False)
    merged_frame = _sort_charges(pd.DataFrame(rows)[EXPORT_COLUMNS])
    return merged_frame.to_csv(index=False)


def write_aggregated_csv(frames: list[pd.DataFrame], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = aggregated_csv_path(output_dir)
    paid_map = _load_paid_map(csv_path)
    if frames:
        charges = prepare_export_dataframe(pd.concat(frames, ignore_index=True))
        charges = _apply_paid(charges, paid_map)
    else:
        charges = pd.DataFrame(columns=EXPORT_COLUMNS)
    charges.to_csv(csv_path, index=False)
    logger.info("Wrote %s (%d row(s))", csv_path.name, len(charges))
    return csv_path


def merge_into_aggregated_csv(
    new_frames: list[pd.DataFrame],
    replaced_bill_dates: set[str],
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = aggregated_csv_path(output_dir)
    paid_map = _load_paid_map(csv_path)
    new_export = _apply_paid(
        prepare_export_dataframe(pd.concat(new_frames, ignore_index=True)),
        paid_map,
    )

    if csv_path.exists() and replaced_bill_dates:
        existing = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        if "bill_date" in existing.columns:
            existing = existing[~existing["bill_date"].isin(replaced_bill_dates)]
        else:
            existing = pd.DataFrame(columns=EXPORT_COLUMNS)
        if PAID_COLUMN not in existing.columns:
            existing[PAID_COLUMN] = ""
        combined = pd.concat([existing, new_export], ignore_index=True)
        charges = _sort_charges(combined)
    else:
        charges = new_export

    charges.to_csv(csv_path, index=False)
    logger.info("Wrote %s (%d row(s))", csv_path.name, len(charges))
    return csv_path
