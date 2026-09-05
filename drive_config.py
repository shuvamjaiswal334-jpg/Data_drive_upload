"""Single source of truth for every Google Drive setting.

Fill in the four folder IDs below once (see DRIVE_SETUP.md for how to get
them). Everything else — file naming, dedup keys, the service-account
credential location — is already wired and does not normally need editing.

Nothing secret lives in this file. The service-account key is read at
runtime from the environment variable named in SERVICE_ACCOUNT_ENV, so the
JSON key is only ever a GitHub secret, never committed.
"""

from __future__ import annotations

# ============================================================
# 1) DRIVE FOLDER IDS   <-- the only thing you must fill in
#
# Open each folder in Google Drive and copy the ID from the URL:
#   https://drive.google.com/drive/folders/1AbCdEf...   <- this part
# ============================================================

DRIVE_FOLDERS = {
    # NSE_INDEX_OPTIONS_<year>.parquet and BSE_INDEX_OPTIONS_<year>.parquet
    "options": "1uO-m2m5l6YOxxt859p2e2M2SI-SoJcVQ",
    # INDEX_FUTURES_<year>.parquet
    "futures": "1fAZi_81R6NlwiIVV7jzdqhNvM5AhE9Ep",
    # Nifty 50.csv, Nifty Bank.csv, Sensex.csv
    "spot": "1EjiKGiCTfT0zGtdOyh_m22X5uSKIiSpy",
}

# Where the raw per-day files are copied for safekeeping. If a per-year
# append ever fails, the year can be rebuilt from these. Leave as the
# placeholder (or set to None) to disable archiving.
DRIVE_ARCHIVE_FOLDER = "1Rg1blEM9rIISgGXGgPThBhy24F1_IotA"


# ============================================================
# 2) SERVICE-ACCOUNT CREDENTIAL
#
# The Drive client reads the service-account key JSON from this env var
# (a GitHub secret in CI). For local runs, set it to the JSON contents or
# to a file path — the Drive client accepts either.
# ============================================================

SERVICE_ACCOUNT_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"

# Preferred for personal Gmail accounts: an OAuth "authorized user" token
# JSON (contents or file path) produced once by drive_oauth_setup.py. When
# set, it takes precedence over the service account. Service accounts have
# no storage quota and cannot CREATE files in a personal My Drive.
OAUTH_TOKEN_ENV = "GOOGLE_OAUTH_TOKEN_JSON"

# Drive API scope. Full drive scope is needed to edit files that already
# exist in folders shared with the service account.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


# ============================================================
# 3) PER-YEAR "SOURCE OF TRUTH" FILE NAMES
#
# {year} is filled from the trading date being appended, so a run in 2026
# targets NSE_INDEX_OPTIONS_2026.parquet, INDEX_FUTURES_2026.parquet, etc.
# These names match the historical archive exactly.
# ============================================================

YEARLY_FILES = {
    "options_nse": "NSE_INDEX_OPTIONS_{year}.parquet",
    "options_bse": "BSE_INDEX_OPTIONS_{year}.parquet",
    "futures": "INDEX_FUTURES_{year}.parquet",
}

# Spot keeps one cumulative CSV per index across all history (no year in
# the name), matching the historical spot files.
SPOT_FILES = {
    "NIFTY": "Nifty 50.csv",
    "BANKNIFTY": "Nifty Bank.csv",
    "SENSEX": "Sensex.csv",
}


# ============================================================
# 4) MAPPING: per-day local file  ->  Drive target
#
# Ties each artifact produced under data/date=YYYY-MM-DD/ to the Drive
# folder, the per-year file it appends into, its format, and the columns
# used to drop duplicate rows on re-runs.
# ============================================================

# Local per-day filenames come from config.py (OPTIONS_FILENAMES,
# FUTURES_FILENAME, spot_filename). Imported lazily inside drive_sync to
# avoid a hard dependency here.

# Natural keys for de-duplication. A row is a duplicate if it matches an
# existing row on every key column — this makes re-running a date safe
# even if the .dates.json sidecar was lost.
DEDUP_KEYS = {
    "options": ["Ticker", "Date", "Time"],
    "futures": ["Ticker", "Date", "Time"],
    "spot": ["timestamp"],
}


def yearly_filename(kind: str, year: int) -> str:
    """Resolve a per-year file name, e.g. yearly_filename('futures', 2026)."""
    try:
        pattern = YEARLY_FILES[kind]
    except KeyError as error:
        raise KeyError(f"Unknown yearly file kind: {kind}") from error
    return pattern.format(year=year)


def folders_configured() -> bool:
    """True only when every folder ID has been filled in (no placeholders)."""
    return all(
        value and not value.startswith("PASTE_")
        for value in DRIVE_FOLDERS.values()
    )


def archive_configured() -> bool:
    """True when the archive folder is set to a real ID."""
    return bool(
        DRIVE_ARCHIVE_FOLDER
        and not DRIVE_ARCHIVE_FOLDER.startswith("PASTE_")
    )
