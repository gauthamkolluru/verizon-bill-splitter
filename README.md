# Verizon Bill Splitter

Parse Verizon wireless bill PDFs with **pdfplumber + pandas** and export one aggregated CSV with bill metadata and per-line charges across all bills.

## Setup

```bash
cd ~/Projects/verizon-bill-splitter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Configure users (optional)

Copy the example mapping and assign each phone number to a person:

```bash
cp config/line_users.yaml.example config/line_users.yaml
```

Edit `config/line_users.yaml`:

```yaml
lines:
  "555-123-4567": Alice
  "555-987-6543": Bob
```

## Monthly workflow

### Option A — Playwright download (recommended)

1. One-time (or when the session expires): sign in with a headed browser:

```bash
python parse_bill.py --login
```

Complete Verizon sign-in / MFA in the browser, then press Enter in the terminal. The session is saved to `config/verizon_storage_state.json` (gitignored).

2. Each month:

```bash
python parse_bill.py --fetch
```

This opens Documents & Receipts, selects Monthly Bills when that category is shown, and clicks the latest bill title. The PDF is saved into `input/`, then any unparsed PDFs are parsed into `output/charges.csv` as usual. If the saved session expired, a headed browser opens again for manual login.

If Verizon changes their bills UI, update the download selectors in `verizon_bill_splitter/verizon_fetch.py` (`DOWNLOAD_SELECTORS`).

### Option B — Manual download

1. **[Open Verizon bills](https://www.verizon.com/digital/nsa/secure/ui/bill/docs/#/)** — sign in and download your bill PDF.
2. Save the PDF to **`input/`** (e.g. `input/MyBill_03.15.2026.pdf`).
3. Run:

```bash
python parse_bill.py
```

With no arguments, the script parses **only PDFs in `input/` that have not been processed yet**. Drop a new bill each month and run the same command.

Check which bills are already done:

```bash
python parse_bill.py --status
```

To parse a specific file or force a re-parse:

```bash
python parse_bill.py input/MyBill_03.15.2026.pdf
python parse_bill.py --force input/MyBill_03.15.2026.pdf
python parse_bill.py --force
```

### Google Drive

Each parse updates `charges.csv` in Google Drive unless you pass `--no-drive`. The upload revises that one file: rows that exist only on Drive are kept, and a `paid` value already on Drive is kept when the local cell is blank.

```bash
cp config/google_drive.yaml.example config/google_drive.yaml
# Add OAuth client JSON as config/google_credentials.json (see CODEBASE_INDEX.md)
python parse_bill.py
```

Default folder: `Verizon Bills` (created under My Drive if needed). The first run opens a browser for consent.

## Output

One aggregated CSV in `output/`:

| File | Contents |
|------|----------|
| `charges.csv` | Bill metadata + every line charge (and account-wide charges) for all parsed bills |

Columns include bill date, billing period, phone, device, charge type (`Line` or `Account`), line amount, bill total, and `paid` (leave blank or mark e.g. `yes` / `no`). Rows are sorted newest bill first. The `paid` column is preserved when you re-run the parser.

Parsed PDFs are tracked in `output/.parsed_manifest.json` (gitignored).

## How it works

The parser looks for the **Bill summary by line** section in your PDF, groups text by row using pdfplumber word positions, pairs each line with its `$` amount, and optionally maps phone numbers to users via YAML.

If Verizon changes the bill layout, adjust the parsing logic in `verizon_bill_splitter/parser.py` (search for `SUMMARY_HEADER`, `SKIP_PHRASES`, and `_parse_summary_rows`).

See `CODEBASE_INDEX.md` for module map and commands.

## Notes

- Bill PDFs stay in `input/` and are gitignored (they contain account info).
- `config/verizon_storage_state.json` is credential-equivalent — keep it local and gitignored.
- Playwright download is optional; you can still drop PDFs into `input/` manually.
