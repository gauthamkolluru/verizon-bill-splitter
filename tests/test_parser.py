from verizon_bill_splitter.parser import (
    merge_charge_csv_texts,
    _extract_name,
    _is_account_wide,
    _parse_summary_coordinate_rows,
    _should_skip,
    _split_line_parts,
    CHARGE_COLUMNS,
    EXPORT_COLUMNS,
    extract_metadata_from_text,
    merge_into_aggregated_csv,
    normalize_phone,
    parse_amount,
    prepare_export_dataframe,
    write_aggregated_csv,
)


def test_normalize_phone():
    assert normalize_phone("(555) 123-4567") == "555-123-4567"
    assert normalize_phone("5551234567") == "555-123-4567"
    assert normalize_phone("Apple iPhone 17 Pro (240 -639-9934)") == "240-639-9934"


def test_parse_amount():
    assert parse_amount("$45.00") == 45.0
    assert parse_amount("$1,234.56") == 1234.56
    assert parse_amount("-$1,147.08") == -1147.08


def test_extract_name():
    assert _extract_name("Jane Doe\niPhone 15") == "Jane Doe"
    assert _extract_name("Jane Doe iPhone 15") == "Jane Doe"
    assert _extract_name("Sai Gautam Kolluru") == "Sai Gautam Kolluru"
    assert _extract_name("Plan charge") is None


def test_split_line_parts():
    name, device, _ = _split_line_parts("Jane Doe\niPhone 15 Pro")
    assert name == "Jane Doe"
    assert device == "iPhone 15 Pro"


def test_should_skip_total_row():
    assert _should_skip("Total:") is True
    assert _should_skip("Account-wide charges & credits") is False


def test_is_account_wide():
    assert _is_account_wide("Account-wide charges & credits") is True


def test_extract_metadata_from_text():
    text = """
    Bill date August 09, 2025
    Account: 687703454-00001
    Invoice: 3175035286
    Billing period: July 10 - August 09, 2025
    Your August bill is $318.03
    Total Current Charges $154.06
    """
    meta = extract_metadata_from_text(text)
    assert meta["bill_date"] == "August 09, 2025"
    assert meta["account_number"] == "687703454-00001"
    assert meta["invoice_number"] == "3175035286"
    assert meta["billing_period_start"] == "July 10"
    assert meta["billing_period_end"] == "August 09"
    assert meta["total_amount_due"] == 318.03
    assert meta["total_current_charges"] == 154.06


def test_parse_summary_rows_simple():
    grouped = [
        [{"text": "Bill summary by line", "x0": 0, "top": 100}],
        [
            {"text": "Jane", "x0": 36, "top": 90},
            {"text": "Doe", "x0": 60, "top": 90},
            {"text": "$45.00", "x0": 310, "top": 90},
        ],
        [
            {"text": "iPhone", "x0": 36, "top": 102},
            {"text": "15", "x0": 70, "top": 102},
            {"text": "(555-123-4567)", "x0": 90, "top": 102},
        ],
    ]
    lines = _parse_summary_coordinate_rows(grouped)
    assert len(lines) == 1
    assert lines[0].amount == 45.0
    assert lines[0].phone == "555-123-4567"
    assert lines[0].charge_type == "line_item"


def test_parse_summary_rows_account_wide():
    grouped = [
        [{"text": "Bill summary by line", "x0": 0, "top": 100}],
        [
            {"text": "Account-wide", "x0": 36, "top": 80},
            {"text": "charges", "x0": 100, "top": 80},
            {"text": "&", "x0": 140, "top": 80},
            {"text": "credits", "x0": 150, "top": 80},
            {"text": "-$12.50", "x0": 300, "top": 80},
        ],
        [
            {"text": "Jane", "x0": 36, "top": 90},
            {"text": "Doe", "x0": 60, "top": 90},
            {"text": "$45.00", "x0": 310, "top": 90},
        ],
    ]
    lines = _parse_summary_coordinate_rows(grouped)
    assert len(lines) == 2
    assert lines[0].charge_type == "account_wide"
    assert lines[0].amount == -12.5
    assert lines[1].amount == 45.0


def _sample_frame(source_file: str, bill_date: str, amount: float):
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "bill_date": bill_date,
                "billing_period_start": None,
                "billing_period_end": None,
                "account_number": None,
                "invoice_number": None,
                "total_amount_due": None,
                "total_current_charges": None,
                "source_file": source_file,
                "charge_type": "line_item",
                "user": "Jane",
                "phone": "555-123-4567",
                "name_on_bill": "Jane",
                "device_or_plan": None,
                "description": "Jane Doe",
                "amount": amount,
            }
        ]
    )[CHARGE_COLUMNS]


def test_write_aggregated_csv(tmp_path):
    output_dir = tmp_path / "output"
    frame_a = _sample_frame("a.pdf", "January 01, 2026", 10.0)
    frame_b = _sample_frame("b.pdf", "February 01, 2026", 20.0)

    csv_path = write_aggregated_csv([frame_b, frame_a], output_dir)

    assert csv_path.name == "charges.csv"
    import pandas as pd

    charges = pd.read_csv(csv_path)
    assert list(charges.columns) == EXPORT_COLUMNS
    assert len(charges) == 2
    assert charges.iloc[0]["bill_date"] == "February 01, 2026"
    assert charges.iloc[1]["bill_date"] == "January 01, 2026"


def test_prepare_export_dataframe_drops_redundant_columns():
    import pandas as pd

    internal = pd.DataFrame(
        [
            {
                "bill_date": "March 01, 2026",
                "billing_period_start": "February 10",
                "billing_period_end": "March 09",
                "account_number": "123-00001",
                "invoice_number": "999",
                "total_amount_due": 150.0,
                "total_current_charges": None,
                "source_file": "bill.pdf",
                "charge_type": "line_item",
                "user": "Jane Doe",
                "phone": "555-123-4567",
                "name_on_bill": "Jane Doe",
                "device_or_plan": "iPhone 15",
                "description": "Jane Doe | iPhone 15",
                "amount": 45.0,
            }
        ]
    )
    export = prepare_export_dataframe(internal)
    assert list(export.columns) == [c for c in EXPORT_COLUMNS if c != "paid"]
    assert export.iloc[0]["billing_period"] == "February 10 - March 09"
    assert export.iloc[0]["charge_type"] == "Line"
    assert export.iloc[0]["bill_total"] == 150.0
    assert "user" not in export.columns
    assert "name_on_bill" not in export.columns


def test_apply_paid_preserves_values_on_rebuild(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    csv_path = output_dir / "charges.csv"
    csv_path.write_text(
        "bill_date,billing_period,phone,device,charge_type,amount,bill_total,paid\n"
        '"January 01, 2026",,555-123-4567,iPhone 15,Line,45.0,150.0,yes\n'
    )

    frame = _sample_frame("old.pdf", "January 01, 2026", 45.0)
    write_aggregated_csv([frame], output_dir)

    import pandas as pd

    charges = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    assert charges.iloc[0]["paid"] == "yes"


def test_merge_into_aggregated_csv_replaces_source(tmp_path):
    output_dir = tmp_path / "output"
    write_aggregated_csv([_sample_frame("old.pdf", "January 01, 2026", 10.0)], output_dir)

    updated = _sample_frame("old.pdf", "January 01, 2026", 99.0)
    new = _sample_frame("new.pdf", "February 01, 2026", 20.0)
    merge_into_aggregated_csv([updated, new], {"January 01, 2026"}, output_dir)

    import pandas as pd

    charges = pd.read_csv(output_dir / "charges.csv")
    assert len(charges) == 2
    jan_rows = charges[charges["bill_date"] == "January 01, 2026"]
    assert len(jan_rows) == 1
    assert jan_rows.iloc[0]["amount"] == 99.0


def test_merge_charge_csv_texts_keeps_drive_rows_and_paid():
    header = "bill_date,billing_period,phone,device,charge_type,amount,bill_total,paid"
    local = (
        f"{header}\n"
        '"October 08, 2026",p,555-111-2222,iPhone,Line,10,100,\n'
    )
    remote = (
        f"{header}\n"
        '"October 08, 2026",old,555-111-2222,Pixel,Line,9,90,yes\n'
        '"September 08, 2026",p,555-111-2222,iPhone,Line,8,80,no\n'
    )
    merged = merge_charge_csv_texts(local, remote)
    import pandas as pd

    frame = pd.read_csv(__import__("io").StringIO(merged), dtype=str, keep_default_na=False)
    october = frame[frame["bill_date"] == "October 08, 2026"].iloc[0]
    september = frame[frame["bill_date"] == "September 08, 2026"]
    assert october["amount"] == "10"
    assert october["device"] == "iPhone"
    assert october["paid"] == "yes"
    assert len(september) == 1
    assert september.iloc[0]["paid"] == "no"
