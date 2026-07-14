from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("verizon_bill_splitter.drive")

DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.file",)
FOLDER_MIME = "application/vnd.google-apps.folder"
CSV_MIME = "text/csv"


@dataclass
class DriveConfig:
    enabled: bool
    credentials_file: Path
    token_file: Path
    folder_path: str
    folder_id: str | None = None
    folder_id_path: str | None = None


def load_drive_config(path: Path | None) -> DriveConfig | None:
    if path is None or not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not read Drive config %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Drive config %s must be a mapping", path)
        return None
    if not raw.get("enabled", False):
        return None

    credentials_file = Path(raw.get("credentials_file", "config/google_credentials.json"))
    token_file = Path(raw.get("token_file", "config/google_token.json"))
    folder_path = str(raw.get("folder_path", "Verizon Bills")).strip()
    if not folder_path:
        logger.warning("Drive config folder_path is empty")
        return None

    return DriveConfig(
        enabled=True,
        credentials_file=credentials_file,
        token_file=token_file,
        folder_path=folder_path,
        folder_id=raw.get("folder_id"),
        folder_id_path=raw.get("folder_id_path"),
    )


def save_drive_config(path: Path, config: DriveConfig) -> None:
    raw: dict[str, Any] = {
        "enabled": config.enabled,
        "credentials_file": str(config.credentials_file),
        "token_file": str(config.token_file),
        "folder_path": config.folder_path,
    }
    if config.folder_id:
        raw["folder_id"] = config.folder_id
    if config.folder_id_path:
        raw["folder_id_path"] = config.folder_id_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _folder_parts(folder_path: str) -> list[str]:
    return [part.strip() for part in folder_path.split("/") if part.strip()]


def _find_folder(service: Any, name: str, parent_id: str) -> str | None:
    query = (
        f"name='{_escape_query_value(name)}' and "
        f"'{parent_id}' in parents and "
        f"mimeType='{FOLDER_MIME}' and trashed=false"
    )
    response = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id)", pageSize=1)
        .execute()
    )
    files = response.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(service: Any, name: str, parent_id: str) -> str:
    metadata = {
        "name": name,
        "mimeType": FOLDER_MIME,
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    folder_id = folder["id"]
    logger.info("Created Drive folder %r (id=%s)", name, folder_id)
    return folder_id


def ensure_folder_path(service: Any, folder_path: str, cached_folder_id: str | None = None) -> str:
    if cached_folder_id:
        try:
            service.files().get(fileId=cached_folder_id, fields="id,trashed").execute()
            logger.debug("Using cached Drive folder id=%s", cached_folder_id)
            return cached_folder_id
        except Exception as exc:
            logger.info("Cached Drive folder unavailable, resolving path again: %s", exc)

    parts = _folder_parts(folder_path)
    if not parts:
        raise ValueError("folder_path must contain at least one folder name")

    parent_id = "root"
    for part in parts:
        existing_id = _find_folder(service, part, parent_id)
        parent_id = existing_id if existing_id else _create_folder(service, part, parent_id)

    logger.info("Resolved Drive folder path %r to id=%s", folder_path, parent_id)
    return parent_id


def _cached_folder_id(config: DriveConfig) -> str | None:
    if not config.folder_id:
        return None
    if config.folder_id_path == config.folder_path:
        return config.folder_id
    return None


def _folder_is_empty(service: Any, folder_id: str) -> bool:
    response = (
        service.files()
        .list(q=f"'{folder_id}' in parents and trashed=false", fields="files(id)", pageSize=1)
        .execute()
    )
    return not response.get("files")


def _trash_drive_item(service: Any, file_id: str, name: str | None = None) -> None:
    service.files().update(fileId=file_id, body={"trashed": True}).execute()
    label = name or file_id
    logger.info("Trashed Drive folder %r (id=%s)", label, file_id)


def _remove_stale_drive_folders(service: Any, stale_folder_id: str) -> None:
    try:
        meta = service.files().get(fileId=stale_folder_id, fields="id,name,parents").execute()
    except Exception as exc:
        logger.warning("Could not inspect old Drive folder %s: %s", stale_folder_id, exc)
        return

    _trash_drive_item(service, stale_folder_id, meta.get("name"))
    for parent_id in meta.get("parents", []):
        if parent_id == "root":
            continue
        try:
            parent = service.files().get(fileId=parent_id, fields="id,name").execute()
        except Exception as exc:
            logger.warning("Could not inspect parent Drive folder %s: %s", parent_id, exc)
            continue
        if parent.get("name") == "Finance" and _folder_is_empty(service, parent_id):
            _trash_drive_item(service, parent_id, "Finance")


def _find_file(service: Any, name: str, parent_id: str) -> str | None:
    query = (
        f"name='{_escape_query_value(name)}' and "
        f"'{parent_id}' in parents and trashed=false"
    )
    response = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id)", pageSize=1)
        .execute()
    )
    files = response.get("files", [])
    return files[0]["id"] if files else None


def upload_csv(service: Any, folder_id: str, local_path: Path) -> str:
    from googleapiclient.http import MediaFileUpload

    if not local_path.is_file():
        raise FileNotFoundError(f"CSV not found: {local_path}")

    media = MediaFileUpload(str(local_path), mimetype=CSV_MIME, resumable=True)
    existing_id = _find_file(service, local_path.name, folder_id)
    if existing_id:
        updated = (
            service.files()
            .update(fileId=existing_id, media_body=media, fields="id,webViewLink")
            .execute()
        )
        logger.info("Updated %s on Google Drive (id=%s)", local_path.name, updated["id"])
        return updated["id"]

    metadata = {"name": local_path.name, "parents": [folder_id]}
    created = (
        service.files()
        .create(body=metadata, media_body=media, fields="id,webViewLink")
        .execute()
    )
    logger.info("Uploaded %s to Google Drive (id=%s)", local_path.name, created["id"])
    return created["id"]


def get_drive_service(config: DriveConfig) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Credentials | None = None
    if config.token_file.exists():
        creds = Credentials.from_authorized_user_file(str(config.token_file), DRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not config.credentials_file.exists():
                raise FileNotFoundError(
                    f"Google OAuth credentials not found: {config.credentials_file}. "
                    "Download OAuth client JSON from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(config.credentials_file), DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        config.token_file.parent.mkdir(parents=True, exist_ok=True)
        config.token_file.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_csv_to_drive(
    local_path: Path,
    config: DriveConfig,
    config_path: Path | None = None,
) -> str:
    service = get_drive_service(config)
    stale_folder_id = (
        config.folder_id if config.folder_id and _cached_folder_id(config) is None else None
    )
    folder_id = ensure_folder_path(service, config.folder_path, _cached_folder_id(config))
    if stale_folder_id and stale_folder_id != folder_id:
        _remove_stale_drive_folders(service, stale_folder_id)

    if folder_id != config.folder_id or config.folder_id_path != config.folder_path:
        config.folder_id = folder_id
        config.folder_id_path = config.folder_path
        if config_path is not None:
            save_drive_config(config_path, config)

    file_id = upload_csv(service, folder_id, local_path)
    return file_id
