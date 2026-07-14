#!/usr/bin/env python3
"""Convenience entry point: python parse_bill.py  (parses unparsed PDFs in input/)"""

from verizon_bill_splitter.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
