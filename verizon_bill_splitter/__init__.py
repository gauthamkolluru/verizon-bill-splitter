"""Parse Verizon wireless bill PDFs into per-line charge summaries."""

from verizon_bill_splitter.parser import BillParseResult, parse_bill_pdf

__all__ = ["BillParseResult", "parse_bill_pdf"]
