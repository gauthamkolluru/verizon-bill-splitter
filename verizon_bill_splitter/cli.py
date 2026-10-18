from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from verizon_bill_splitter.drive import (
    download_charges_csv_text,
    load_drive_config,
    upload_csv_to_drive,
)
from verizon_bill_splitter.logging_config import configure_logging
from verizon_bill_splitter.manifest import classify_pdfs, is_parsed, list_unparsed_pdfs, mark_parsed
from verizon_bill_splitter.parser import (
    aggregated_csv_path,
    load_user_map,
    merge_charge_csv_texts,
    merge_into_aggregated_csv,
    parse_bill_pdf,
    write_aggregated_csv,
)
from verizon_bill_splitter.verizon_fetch import (
    VERIZON_BILLS_URL,
    VerizonFetchError,
    default_storage_state_path,
    fetch_missing_bills,
    latest_bill_month,
    login_and_save_session,
)

logger = logging.getLogger("verizon_bill_splitter.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse Verizon bill PDFs and export aggregated per-line charges to a single CSV.",
        epilog=f"Download bills: {VERIZON_BILLS_URL}",
    )
    parser.add_argument(
        "pdf",
        nargs="*",
        type=Path,
        help="PDF path(s). With no arguments, parses unparsed PDFs in --input-dir.",
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        type=Path,
        default=Path("input"),
        help="Folder for monthly bill PDFs (default: input/)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Directory for CSV exports (default: output/)",
    )
    parser.add_argument(
        "-u",
        "--users",
        type=Path,
        default=Path("config/line_users.yaml"),
        help="YAML file mapping phone numbers to user names (default: config/line_users.yaml)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-parse PDFs even if already processed; with no PDF args, re-parses all PDFs in --input-dir",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Download the latest bill PDF via Playwright into --input-dir, then parse",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a headed browser to sign in to Verizon and save the session, then exit",
    )
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=None,
        help="Playwright storage state path (default: config/verizon_storage_state.json)",
    )
    parser.add_argument(
        "--drive-config",
        type=Path,
        default=Path("config/google_drive.yaml"),
        help="Google Drive upload config (default: config/google_drive.yaml)",
    )
    parser.add_argument(
        "--no-drive",
        action="store_true",
        help="Skip Google Drive upload even when enabled in config",
    )
    parser.add_argument(
        "--upload-drive",
        action="store_true",
        help="Accepted for compatibility; Drive upload already runs unless --no-drive",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="List parsed and unparsed PDFs in --input-dir, then exit",
    )
    return parser


def _print_bill_status(input_dir: Path, output_dir: Path) -> int:
    status = classify_pdfs(input_dir, output_dir)
    if not status.all_pdfs:
        print(f"No PDFs in {input_dir}")
        return 0

    print(f"Parsed ({len(status.parsed)}):")
    for pdf in status.parsed:
        print(f"  {pdf.name}")
    print(f"Unparsed ({len(status.unparsed)}):")
    for pdf in status.unparsed:
        print(f"  {pdf.name}")
    return 0


def _resolve_pdfs(args: argparse.Namespace) -> list[Path]:
    if args.pdf:
        return list(args.pdf)
    if args.force:
        input_dir = args.input_dir
        if not input_dir.is_dir():
            return []
        return sorted(input_dir.glob("*.pdf"))
    return list_unparsed_pdfs(args.input_dir, args.output_dir)


def _parse_one(
    pdf: Path, args: argparse.Namespace, user_map: dict[str, str]
) -> tuple[int, pd.DataFrame | None]:
    if not pdf.exists():
        logger.error("PDF not found: %s", pdf)
        return 1, None

    if not args.force and is_parsed(pdf, args.output_dir):
        logger.info("Already parsed (use --force to redo): %s", pdf.name)
        return 0, None

    try:
        result = parse_bill_pdf(pdf)
    except ValueError as exc:
        logger.error("Parse failed for %s: %s", pdf.name, exc)
        return 1, None

    charges = result.to_dataframe(user_map=user_map)

    print(f"Parsed {len(result.lines)} line(s) from {pdf.name}")
    if result.bill_date:
        print(f"Bill date: {result.bill_date}")
    if result.account_number:
        print(f"Account: {result.account_number}")
    return 0, charges


def _maybe_upload_to_drive(args: argparse.Namespace) -> int:
    if args.no_drive:
        return 0

    drive_config = load_drive_config(args.drive_config)
    if drive_config is None:
        logger.info("Drive upload skipped because it is disabled in %s", args.drive_config)
        return 0

    csv_path = aggregated_csv_path(args.output_dir)
    if not csv_path.exists():
        logger.error("No CSV to upload: %s", csv_path)
        return 1

    try:
        remote_text = download_charges_csv_text(drive_config, args.drive_config)
        merged = merge_charge_csv_texts(csv_path.read_text(), remote_text)
        csv_path.write_text(merged)
        file_id = upload_csv_to_drive(csv_path, drive_config, config_path=args.drive_config)
    except FileNotFoundError as exc:
        logger.error("Drive upload setup incomplete: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Drive upload failed: %s", exc)
        return 1

    print(f"Google Drive: uploaded {csv_path.name} (id={file_id})")
    return 0


def _storage_state_path(args: argparse.Namespace) -> Path:
    return args.storage_state or default_storage_state_path()


def _run_login(args: argparse.Namespace) -> int:
    try:
        path = login_and_save_session(_storage_state_path(args))
    except VerizonFetchError as exc:
        logger.error("%s", exc)
        return 1
    print(f"Saved Verizon session: {path}")
    return 0


def _csv_cutoff(args: argparse.Namespace):
    text = None
    drive_config = load_drive_config(args.drive_config)
    if drive_config is not None:
        try:
            text = download_charges_csv_text(drive_config, args.drive_config)
        except Exception as exc:
            logger.warning("Could not read Drive charges.csv: %s", exc)
    if not text:
        local_csv = aggregated_csv_path(args.output_dir)
        if local_csv.is_file():
            try:
                text = local_csv.read_text()
            except OSError as exc:
                logger.warning("Could not read %s: %s", local_csv, exc)
    if not text:
        logger.info("No charges.csv cutoff; downloading every View PDF on the page")
        return None
    latest = latest_bill_month(text)
    if latest is None:
        logger.info("charges.csv has no bill date; downloading every View PDF on the page")
        return None
    logger.info("CSV cutoff bill month: %04d-%02d", latest.year, latest.month)
    return latest


def _run_fetch(args: argparse.Namespace) -> int:
    try:
        saved = fetch_missing_bills(
            args.input_dir,
            _storage_state_path(args),
            force_login=False,
            latest_month=_csv_cutoff(args),
        )
    except VerizonFetchError as exc:
        logger.error("%s", exc)
        return 1
    if not saved:
        print("No bills newer than the CSV cutoff.")
        return 0
    for dest in saved:
        print(f"Fetched bill: {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    if args.status:
        return _print_bill_status(args.input_dir, args.output_dir)
    if args.login:
        return _run_login(args)
    if args.fetch:
        fetch_code = _run_fetch(args)
        if fetch_code != 0:
            return fetch_code
    pdfs = _resolve_pdfs(args)

    if not pdfs:
        if args.upload_drive:
            return _maybe_upload_to_drive(args)
        if not args.pdf:
            logger.info(
                "No unparsed PDFs in %s. Drop new bills there and run again.",
                args.input_dir,
            )
            print(f"Download bills: {VERIZON_BILLS_URL}")
            return _maybe_upload_to_drive(args)
        logger.error("No PDF files specified")
        return 1

    user_map = load_user_map(args.users)
    exit_code = 0
    frames: list[pd.DataFrame] = []
    replaced_bill_dates: set[str] = set()
    parsed_pdfs: list[Path] = []

    for pdf in pdfs:
        code, charges = _parse_one(pdf, args, user_map)
        if code != 0:
            exit_code = 1
        elif charges is not None:
            frames.append(charges)
            bill_dates = charges["bill_date"].dropna().unique()
            replaced_bill_dates.update(str(d) for d in bill_dates)
            parsed_pdfs.append(pdf)

    if frames:
        if args.force and not args.pdf:
            csv_path = write_aggregated_csv(frames, args.output_dir)
        else:
            csv_path = merge_into_aggregated_csv(frames, replaced_bill_dates, args.output_dir)
        for pdf in parsed_pdfs:
            mark_parsed(pdf, csv_path, args.output_dir)
        print(f"CSV: {csv_path}")

    upload_code = _maybe_upload_to_drive(args)
    return exit_code or upload_code


if __name__ == "__main__":
    raise SystemExit(main())
