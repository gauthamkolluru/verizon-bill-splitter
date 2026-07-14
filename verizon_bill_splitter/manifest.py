from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("verizon_bill_splitter.manifest")

MANIFEST_NAME = ".parsed_manifest.json"


@dataclass(frozen=True)
class BillParseStatus:
    parsed: tuple[Path, ...]
    unparsed: tuple[Path, ...]

    @property
    def all_pdfs(self) -> tuple[Path, ...]:
        return self.parsed + self.unparsed


def manifest_path(output_dir: Path) -> Path:
    return output_dir / MANIFEST_NAME


def _manifest_key(pdf_path: Path) -> str:
    return pdf_path.name


def _file_signature(path: Path) -> dict[str, float | int]:
    stat = path.stat()
    return {"mtime": stat.st_mtime, "size": stat.st_size}


def _normalize_manifest(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        filename = Path(key).name
        existing = normalized.get(filename)
        if existing is None:
            normalized[filename] = entry
            continue
        if entry.get("mtime", 0) >= existing.get("mtime", 0):
            normalized[filename] = entry
    return normalized


def load_manifest(output_dir: Path) -> dict[str, dict[str, Any]]:
    path = manifest_path(output_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read manifest %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    normalized = _normalize_manifest(data)
    if normalized != data:
        logger.info("Normalized manifest keys to PDF filenames")
        save_manifest(output_dir, normalized)
    return normalized


def save_manifest(output_dir: Path, manifest: dict[str, dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path(output_dir).write_text(json.dumps(manifest, indent=2) + "\n")


def is_parsed(pdf_path: Path, output_dir: Path) -> bool:
    manifest = load_manifest(output_dir)
    entry = manifest.get(_manifest_key(pdf_path))
    if not entry:
        return False
    output = Path(entry.get("output", ""))
    if not output.exists():
        logger.info("Output missing for %s; will re-parse", pdf_path.name)
        return False
    try:
        current = _file_signature(pdf_path)
    except OSError as exc:
        logger.warning("Could not stat %s: %s", pdf_path, exc)
        return False
    if current != {"mtime": entry.get("mtime"), "size": entry.get("size")}:
        logger.info("PDF changed since last parse: %s", pdf_path.name)
        return False
    return True


def mark_parsed(pdf_path: Path, output_path: Path, output_dir: Path) -> None:
    manifest = load_manifest(output_dir)
    manifest[_manifest_key(pdf_path)] = {
        **_file_signature(pdf_path),
        "output": str(output_path.resolve()),
    }
    save_manifest(output_dir, manifest)


def classify_pdfs(input_dir: Path, output_dir: Path) -> BillParseStatus:
    if not input_dir.is_dir():
        return BillParseStatus(parsed=(), unparsed=())
    pdfs = sorted(input_dir.glob("*.pdf"))
    parsed: list[Path] = []
    unparsed: list[Path] = []
    for pdf in pdfs:
        if is_parsed(pdf, output_dir):
            parsed.append(pdf)
        else:
            unparsed.append(pdf)
    return BillParseStatus(parsed=tuple(parsed), unparsed=tuple(unparsed))


def list_unparsed_pdfs(input_dir: Path, output_dir: Path) -> list[Path]:
    status = classify_pdfs(input_dir, output_dir)
    if status.parsed:
        logger.info(
            "Skipping %d already-parsed PDF(s) in %s: %s",
            len(status.parsed),
            input_dir,
            ", ".join(pdf.name for pdf in status.parsed),
        )
    if status.unparsed:
        logger.info(
            "%d unparsed PDF(s) in %s: %s",
            len(status.unparsed),
            input_dir,
            ", ".join(pdf.name for pdf in status.unparsed),
        )
    return list(status.unparsed)
