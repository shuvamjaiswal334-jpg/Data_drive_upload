"""Thin Google Drive wrapper for the daily append job.

Wraps just the operations drive_sync needs, so the rest of the codebase
never touches the raw Drive API:

    find_file(folder_id, name)      -> file id or None
    download_file(file_id, path)    -> download to a local path
    replace_file(file_id, path)     -> overwrite an existing file's bytes
    upload_file(folder_id, name, path) -> create a new file in a folder
    upsert_file(folder_id, name, path) -> replace if present, else create

Auth uses a service-account key read from the environment (a GitHub
secret in CI). The env value may be either the JSON contents or a path to
a JSON file — both are accepted so the same code works locally and in CI.

Personal-Gmail note: a service account cannot own files in a personal
Drive, but it can edit files inside a folder that has been shared with the
service-account email. replace_file() overwrites content in place (same
file id), so ownership stays with you.
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

import drive_config

logger = logging.getLogger(__name__)


class DriveError(RuntimeError):
    """Any failure talking to Google Drive."""


def _read_json_env(env_name: str):
    """Return parsed JSON from an env var holding inline JSON or a file path.

    Returns None when the variable is unset/blank.
    """
    raw = os.getenv(env_name)
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    if not raw.startswith("{"):
        key_path = Path(raw)
        if not key_path.exists():
            raise DriveError(
                f"{env_name} looks like a path but the file does not "
                f"exist: {key_path}"
            )
        raw = key_path.read_text(encoding="utf-8")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise DriveError(f"{env_name} is not valid JSON: {error}") from error


def _load_credentials():
    """Build Drive credentials from the environment.

    Two modes, checked in order:

    1. OAuth user credentials (OAUTH_TOKEN_ENV) — an "authorized user" JSON
       produced once by drive_oauth_setup.py. Files are created as YOU and
       count against your own quota. **Required for personal Gmail**:
       Google no longer lets service accounts create files in a personal
       My Drive ("Service Accounts do not have storage quota").

    2. Service account (SERVICE_ACCOUNT_ENV) — works for Google Workspace
       shared drives, or for updating (not creating) existing files.
    """
    user_info = _read_json_env(drive_config.OAUTH_TOKEN_ENV)
    if user_info:
        credentials = UserCredentials.from_authorized_user_info(
            user_info, scopes=drive_config.DRIVE_SCOPES
        )
        if not credentials.valid:
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                raise DriveError(
                    f"{drive_config.OAUTH_TOKEN_ENV} has no usable refresh "
                    "token. Re-run drive_oauth_setup.py."
                )
        logger.info("Drive auth: OAuth user credentials.")
        return credentials

    sa_info = _read_json_env(drive_config.SERVICE_ACCOUNT_ENV)
    if sa_info:
        logger.info("Drive auth: service account.")
        return service_account.Credentials.from_service_account_info(
            sa_info, scopes=drive_config.DRIVE_SCOPES
        )

    raise DriveError(
        f"Neither {drive_config.OAUTH_TOKEN_ENV} nor "
        f"{drive_config.SERVICE_ACCOUNT_ENV} is set. For a personal Gmail "
        "Drive, run drive_oauth_setup.py and set "
        f"{drive_config.OAUTH_TOKEN_ENV}."
    )


class DriveClient:
    """Minimal Drive helper scoped to the operations the sync needs."""

    def __init__(self, service=None):
        if service is not None:
            # Injected service (used by tests / mocks).
            self.service = service
        else:
            credentials = _load_credentials()
            # cache_discovery=False avoids a noisy warning on some setups.
            self.service = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )

    # ---- lookup ------------------------------------------------

    def find_file(self, folder_id: str, name: str):
        """Return the id of a non-trashed file named `name` in `folder_id`.

        Returns None if not found. Raises DriveError on more than one
        match, since an ambiguous target would make appends unsafe.
        """
        safe_name = name.replace("'", "\\'")
        query = (
            f"name = '{safe_name}' and "
            f"'{folder_id}' in parents and trashed = false"
        )
        try:
            response = self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        except HttpError as error:
            raise DriveError(f"find_file failed for {name}: {error}") from error

        files = response.get("files", [])
        if not files:
            return None
        if len(files) > 1:
            raise DriveError(
                f"Ambiguous: {len(files)} files named '{name}' in folder "
                f"{folder_id}. Resolve the duplicates in Drive."
            )
        return files[0]["id"]

    # ---- download ----------------------------------------------

    def download_file(self, file_id: str, destination: Path):
        """Download a file's bytes to a local path."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        request = self.service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        with destination.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()

        return destination

    # ---- upload / replace --------------------------------------

    def replace_file(self, file_id: str, source: Path):
        """Overwrite the content of an existing Drive file (same id)."""
        source = Path(source)
        media = MediaFileUpload(str(source), resumable=True)
        try:
            self.service.files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
        except HttpError as error:
            raise DriveError(f"replace_file failed: {error}") from error
        return file_id

    def upload_file(self, folder_id: str, name: str, source: Path):
        """Create a new file named `name` in `folder_id`."""
        source = Path(source)
        metadata = {"name": name, "parents": [folder_id]}
        media = MediaFileUpload(str(source), resumable=True)
        try:
            created = self.service.files().create(
                body=metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
        except HttpError as error:
            if "storageQuotaExceeded" in str(error):
                raise DriveError(
                    f"upload_file failed for {name}: service accounts cannot "
                    "create files in a personal My Drive. Use OAuth user "
                    "credentials instead — run drive_oauth_setup.py and set "
                    f"{drive_config.OAUTH_TOKEN_ENV} (see DRIVE_SETUP.md)."
                ) from error
            raise DriveError(f"upload_file failed for {name}: {error}") from error
        return created["id"]

    def upsert_file(self, folder_id: str, name: str, source: Path):
        """Replace the file if it already exists, otherwise create it.

        Returns (file_id, created) where created is True for a new file.
        """
        existing = self.find_file(folder_id, name)
        if existing:
            self.replace_file(existing, source)
            return existing, False
        new_id = self.upload_file(folder_id, name, source)
        return new_id, True

    # ---- misc --------------------------------------------------

    def read_text(self, folder_id: str, name: str):
        """Return a small text file's contents, or None if absent.

        Used for the .dates.json sidecars. Kept separate from
        download_file so callers can treat "missing sidecar" as "no dates
        ingested yet".
        """
        file_id = self.find_file(folder_id, name)
        if not file_id:
            return None
        buffer = io.BytesIO()
        request = self.service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buffer.getvalue().decode("utf-8")
