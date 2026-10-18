# Verizon Bill Splitter — codebase index

## What

Parses Verizon wireless bill PDFs (manual drop or Playwright download) and exports one aggregated CSV with bill metadata and per-line charges across all bills.

## Where

| Path | Role |
|------|------|
| `parse_bill.py` | CLI entry point |
| `verizon_bill_splitter/cli.py` | Argument parsing, batch/unparsed workflow, `--fetch` / `--login` |
| `verizon_bill_splitter/parser.py` | pdfplumber parsing, CSV export |
| `verizon_bill_splitter/manifest.py` | Tracks parsed PDFs so reruns skip completed files |
| `verizon_bill_splitter/verizon_fetch.py` | Playwright download + local browser session (`storage_state`) |
| `verizon_bill_splitter/drive.py` | Google Drive folder creation and CSV upload |
| `verizon_bill_splitter/logging_config.py` | Central logging setup |
| `config/line_users.yaml.example` | Template for phone→user map |
| `config/google_drive.yaml.example` | Template for Google Drive upload |
| `config/line_users.yaml` | Local map (gitignored) |
| `config/google_drive.yaml` | Local Drive upload settings (gitignored) |
| `config/google_credentials.json` | OAuth client JSON from Google Cloud (gitignored) |
| `config/google_token.json` | Saved OAuth token after first consent (gitignored) |
| `config/verizon_storage_state.json` | Playwright Verizon session (gitignored) |
| `input/` | **Drop monthly bill PDFs here** (gitignored) |
| `output/` | CSV exports + `.parsed_manifest.json` (gitignored) |
| `tests/` | pytest unit tests |

## How

### Monthly workflow

**Playwright (optional):**

```bash
source .venv/bin/activate
# First time / expired session — headed browser, then press Enter
python parse_bill.py --login
# Each month: download into input/, then parse unparsed PDFs
python parse_bill.py --fetch
```

Session file: `config/verizon_storage_state.json`. `--fetch` opens Documents & Receipts, selects Monthly Bills, and clicks the latest bill title. If Verizon changes the bills UI, edit `DOWNLOAD_SELECTORS` in `verizon_bill_splitter/verizon_fetch.py`.

**Manual:**

1. Open [Verizon bills page](https://www.verizon.com/digital/nsa/secure/ui/bill/docs/#/) and sign in.
2. Download the bill PDF and save it to `input/`.
3. Run:

```bash
source .venv/bin/activate
python parse_bill.py
```

Only PDFs not yet recorded in `output/.parsed_manifest.json` are parsed. Keys are PDF **filenames** (not absolute paths), so moving the project folder does not reset progress. Changed files (new download) are detected via size/mtime and re-parsed automatically.

```bash
# See which bills are already parsed vs pending
python parse_bill.py --status
```

### Commands

```bash
# Parse all unparsed PDFs in input/
python parse_bill.py

# Download latest bill via Playwright, then parse
python parse_bill.py --fetch

# Save / refresh Verizon browser session (headed login)
python parse_bill.py --login

# List parsed vs unparsed PDFs without parsing
python parse_bill.py --status

# Parse specific file(s)
python parse_bill.py input/MyBill_03.15.2026.pdf

# Re-parse even if already done
python parse_bill.py --force input/MyBill_03.15.2026.pdf

# Re-parse all PDFs in input/ and rebuild the aggregated CSV
python parse_bill.py --force

# Parse, then update charges.csv on Google Drive (default)
python parse_bill.py

# Skip the Drive update for one run
python parse_bill.py --no-drive

# Tests
pytest
```

### Google Drive upload

1. Copy `config/google_drive.yaml.example` to `config/google_drive.yaml`.
2. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable **Google Drive API**, and create an **OAuth client ID** (Desktop app).
3. Download the client JSON to `config/google_credentials.json`.
4. Run `python parse_bill.py` once; complete the browser consent flow.

Each run updates `charges.csv` in `Verizon Bills` under My Drive (the folder is created if missing). Rows that exist only on Drive stay, and a Drive `paid` value is kept when the local cell is blank. Use `--no-drive` to skip the update for a single run. Set `enabled: false` in `config/google_drive.yaml` to turn upload off.

### Output

Single aggregated file: `output/charges.csv` with columns:

| Column | Description |
|--------|-------------|
| `bill_date` | Statement date (newest bills first) |
| `billing_period` | Service period covered by the bill |
| `phone` | Line phone number |
| `device` | Device or plan on the line |
| `charge_type` | `Line` or `Account` (account-wide credits/charges) |
| `amount` | Charge for this row |
| `bill_total` | Total amount due for that bill |
| `paid` | Your payment status (edit freely; preserved across re-parses) |

### Parser tuning

If Verizon changes bill layout, edit `verizon_bill_splitter/parser.py`:

- `SUMMARY_HEADER` — section anchor text
- `SKIP_PHRASES` — rows to ignore
- `_parse_summary_rows` — row/amount pairing logic

## Build / deps

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Stack: Python 3.12+, pdfplumber, pandas, openpyxl, pyyaml, google-api-python-client, playwright.
