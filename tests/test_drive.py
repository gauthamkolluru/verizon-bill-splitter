from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from verizon_bill_splitter.drive import (
    DriveConfig,
    _escape_query_value,
    _folder_parts,
    download_charges_csv,
    ensure_folder_path,
    load_drive_config,
    save_drive_config,
    upload_csv,
    upload_csv_to_drive,
)


def test_escape_query_value():
    assert _escape_query_value("Bob's Bills") == "Bob\\'s Bills"


def test_folder_parts():
    assert _folder_parts("Finance/Verizon Bills") == ["Finance", "Verizon Bills"]
    assert _folder_parts("/Finance//Verizon/") == ["Finance", "Verizon"]


def test_load_drive_config_defaults_enabled_when_key_missing(tmp_path: Path):
    path = tmp_path / "google_drive.yaml"
    path.write_text("folder_path: Verizon Bills\n")
    config = load_drive_config(path)
    assert config is not None
    assert config.enabled is True
    assert config.folder_path == "Verizon Bills"


def test_load_drive_config_missing_file_uses_defaults(tmp_path: Path):
    config = load_drive_config(tmp_path / "missing.yaml")
    assert config is not None
    assert config.enabled is True
    assert config.credentials_file == Path("config/google_credentials.json")
    assert config.folder_path == "Verizon Bills"


def test_load_drive_config_disabled(tmp_path: Path):
    path = tmp_path / "google_drive.yaml"
    path.write_text("enabled: false\n")
    assert load_drive_config(path) is None


def test_load_drive_config_enabled(tmp_path: Path):
    path = tmp_path / "google_drive.yaml"
    path.write_text(
        "enabled: true\n"
        "folder_path: Verizon Bills\n"
        "folder_id: abc123\n"
        "folder_id_path: Verizon Bills\n"
    )
    config = load_drive_config(path)
    assert config is not None
    assert config.folder_path == "Verizon Bills"
    assert config.folder_id == "abc123"
    assert config.folder_id_path == "Verizon Bills"


def test_cached_folder_id_ignores_stale_path():
    config = DriveConfig(
        enabled=True,
        credentials_file=Path("creds.json"),
        token_file=Path("token.json"),
        folder_path="Verizon Bills",
        folder_id="old-id",
        folder_id_path="Finance/Verizon Bills",
    )
    from verizon_bill_splitter.drive import _cached_folder_id

    assert _cached_folder_id(config) is None


def test_save_drive_config_round_trip(tmp_path: Path):
    path = tmp_path / "google_drive.yaml"
    config = DriveConfig(
        enabled=True,
        credentials_file=Path("config/google_credentials.json"),
        token_file=Path("config/google_token.json"),
        folder_path="Verizon Bills",
        folder_id="folder-1",
    )
    save_drive_config(path, config)
    loaded = load_drive_config(path)
    assert loaded is not None
    assert loaded.folder_id == "folder-1"


def test_ensure_folder_path_uses_cache():
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = {"id": "cached", "trashed": False}

    folder_id = ensure_folder_path(service, "Verizon Bills", cached_folder_id="cached")

    assert folder_id == "cached"
    service.files.return_value.list.assert_not_called()


def test_ensure_folder_path_creates_missing_folders():
    service = MagicMock()
    list_mock = service.files.return_value.list.return_value
    list_mock.execute.return_value = {"files": []}
    create_mock = service.files.return_value.create.return_value
    create_mock.execute.return_value = {"id": "verizon-id"}

    folder_id = ensure_folder_path(service, "Verizon Bills")

    assert folder_id == "verizon-id"
    assert create_mock.execute.call_count == 1


def test_download_charges_csv_reads_existing_file():
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "file-1"}]
    }

    class _Downloader:
        def __init__(self, buffer, request):
            self.buffer = buffer
            assert request == "media-request"

        def next_chunk(self):
            self.buffer.write(b"bill_date,amount\n03/15/2026,10\n")
            return None, True

    service.files.return_value.get_media.return_value = "media-request"
    with patch("googleapiclient.http.MediaIoBaseDownload", _Downloader):
        text = download_charges_csv(service, "folder-1")

    assert text == "bill_date,amount\n03/15/2026,10\n"
    service.files.return_value.get_media.assert_called_once_with(fileId="file-1")


def test_download_charges_csv_missing_file():
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    assert download_charges_csv(service, "folder-1") is None
    service.files.return_value.get_media.assert_not_called()


def test_upload_csv_updates_existing_file(tmp_path: Path):
    csv_path = tmp_path / "charges.csv"
    csv_path.write_text("bill_date,amount\n")

    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": [{"id": "file-1"}]}
    service.files.return_value.update.return_value.execute.return_value = {"id": "file-1"}

    with patch("googleapiclient.http.MediaFileUpload"):
        file_id = upload_csv(service, "folder-1", csv_path)

    assert file_id == "file-1"
    service.files.return_value.update.assert_called_once()
    service.files.return_value.create.assert_not_called()


def test_upload_csv_to_drive_persists_folder_id(tmp_path: Path):
    csv_path = tmp_path / "charges.csv"
    csv_path.write_text("bill_date,amount\n")
    config_path = tmp_path / "google_drive.yaml"
    config = DriveConfig(
        enabled=True,
        credentials_file=tmp_path / "creds.json",
        token_file=tmp_path / "token.json",
        folder_path="Verizon Bills",
    )
    save_drive_config(config_path, config)

    service = MagicMock()
    with (
        patch("verizon_bill_splitter.drive.get_drive_service", return_value=service),
        patch(
            "verizon_bill_splitter.drive.ensure_folder_path",
            return_value="resolved-folder",
        ) as ensure_mock,
        patch("verizon_bill_splitter.drive.upload_csv", return_value="file-1") as upload_mock,
    ):
        file_id = upload_csv_to_drive(csv_path, config, config_path=config_path)

    assert file_id == "file-1"
    ensure_mock.assert_called_once_with(service, "Verizon Bills", None)
    upload_mock.assert_called_once_with(service, "resolved-folder", csv_path)

    reloaded = load_drive_config(config_path)
    assert reloaded is not None
    assert reloaded.folder_id == "resolved-folder"
    assert reloaded.folder_id_path == "Verizon Bills"


def test_upload_csv_to_drive_does_not_trash_stale_folder(tmp_path: Path):
    csv_path = tmp_path / "charges.csv"
    csv_path.write_text("bill_date,amount\n")
    config = DriveConfig(
        enabled=True,
        credentials_file=tmp_path / "creds.json",
        token_file=tmp_path / "token.json",
        folder_path="Verizon Bills",
        folder_id="old-folder",
        folder_id_path="Finance/Verizon Bills",
    )
    service = MagicMock()
    with (
        patch("verizon_bill_splitter.drive.get_drive_service", return_value=service),
        patch("verizon_bill_splitter.drive.ensure_folder_path", return_value="new-folder"),
        patch("verizon_bill_splitter.drive.upload_csv", return_value="file-1"),
        patch("verizon_bill_splitter.drive._remove_stale_drive_folders") as trash_mock,
    ):
        upload_csv_to_drive(csv_path, config)

    trash_mock.assert_not_called()
