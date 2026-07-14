from pathlib import Path

import pytest

from verizon_bill_splitter.manifest import (
    classify_pdfs,
    is_parsed,
    list_unparsed_pdfs,
    load_manifest,
    mark_parsed,
)


def test_mark_and_detect_parsed(tmp_path: Path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    pdf = input_dir / "MyBill.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    csv_path = output_dir / "charges.csv"
    csv_path.write_text("bill_date,amount\n")

    assert is_parsed(pdf, output_dir) is False
    mark_parsed(pdf, csv_path, output_dir)
    assert is_parsed(pdf, output_dir) is True
    assert "MyBill.pdf" in load_manifest(output_dir)


def test_changed_pdf_is_unparsed(tmp_path: Path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    pdf = input_dir / "MyBill.pdf"
    pdf.write_bytes(b"version-1")
    csv_path = output_dir / "charges.csv"
    csv_path.write_text("bill_date,amount\n")
    mark_parsed(pdf, csv_path, output_dir)

    pdf.write_bytes(b"version-2-updated")
    assert is_parsed(pdf, output_dir) is False


def test_list_unparsed_pdfs(tmp_path: Path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    old_pdf = input_dir / "old.pdf"
    new_pdf = input_dir / "new.pdf"
    old_pdf.write_bytes(b"old")
    new_pdf.write_bytes(b"new")

    csv_path = output_dir / "old.csv"
    csv_path.write_text("bill_date,amount\n")
    mark_parsed(old_pdf, csv_path, output_dir)

    unparsed = list_unparsed_pdfs(input_dir, output_dir)
    assert unparsed == [new_pdf]


def test_classify_pdfs(tmp_path: Path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    done = input_dir / "done.pdf"
    pending = input_dir / "pending.pdf"
    done.write_bytes(b"done")
    pending.write_bytes(b"pending")

    csv_path = output_dir / "charges.csv"
    csv_path.write_text("bill_date,amount\n")
    mark_parsed(done, csv_path, output_dir)

    status = classify_pdfs(input_dir, output_dir)
    assert status.parsed == (done,)
    assert status.unparsed == (pending,)


def test_migrates_absolute_path_manifest_keys(tmp_path: Path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    pdf = input_dir / "MyBill.pdf"
    pdf.write_bytes(b"same-content")
    csv_path = output_dir / "charges.csv"
    csv_path.write_text("bill_date,amount\n")

    legacy_key = f"/old/project/input/{pdf.name}"
    output_dir.joinpath(".parsed_manifest.json").write_text(
        f'{{"{legacy_key}": {{"mtime": {pdf.stat().st_mtime}, "size": {pdf.stat().st_size}, '
        f'"output": "{csv_path}"}}}}\n'
    )

    assert is_parsed(pdf, output_dir) is True
    assert "MyBill.pdf" in load_manifest(output_dir)
    assert legacy_key not in load_manifest(output_dir)
