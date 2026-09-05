"""Append one trading day's local outputs into the per-year "source of
truth" files on Google Drive.



For each artifact produced under data/date=YYYY-MM-DD/ this does:

    1. resolve the target per-year Drive file (create if missing)
    2. read its .dates.json sidecar (the list of dates already ingested)
    3. if the date is already ingested -> skip (idempotent)
    4. download the current per-year file
    5. concatenate the new day, drop duplicate rows on natural keys
    6. write locally, read back, verify row count
    7. atomically replace the file's bytes on Drive (same file id)
    8. update the .dates.json sidecar on Drive
    9. copy the raw per-day file into the dated Drive archive

Design choices:
  * Idempotency is belt-and-suspenders: the sidecar prevents re-appending
    a date, and the natural-key dedup prevents duplicate rows even if the
    sidecar is lost. Re-running a day is always safe.
  * The old per-year file is never destroyed. replace_file overwrites in
    place only after the new content is verified locally; if anything
    fails before that, Drive still holds last-good.
  * The CSV append/dedup core (merge_and_dedup) is pure and Drive-free so
    it can be unit tested without any network.

Options is the largest file (~600-800 MB/year). At that size the
download+reupload dominates this step but is still small next to the FYERS
collection. Parquet merge/dedup uses DuckDB so the full year is streamed
from parquet instead of held in pandas memory.
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
from dotenv import load_dotenv

import config
import drive_config
from drive_client import DriveClient, DriveError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("drive_sync")


IST = config.IST


# ============================================================
# WORK ITEMS: what per-day file goes into which per-year target
# ============================================================

class SyncItem:
    """One (local per-day file) -> (per-year Drive file) mapping."""

    def __init__(self, kind, fmt, folder_key, local_path, drive_name, dedup_keys):
        self.kind = kind              # options / futures / spot
        self.fmt = fmt                # "parquet" or "csv"
        self.folder_key = folder_key  # key into drive_config.DRIVE_FOLDERS
        self.local_path = Path(local_path)
        self.drive_name = drive_name  # per-year / cumulative file name
        self.dedup_keys = dedup_keys


def build_sync_items(trading_date: date):
    """Build the list of files to sync for a trading date.

    Only items whose local per-day file actually exists are included, so a
    day where (say) BSE options produced nothing is simply not synced.
    """
    directory = config.DATA_DIR / f"date={trading_date.isoformat()}"
    year = trading_date.year
    items = []

    # Options: split NSE / BSE -> matching per-year enriched file.
    options_targets = {
        "NSE": ("options_nse", drive_config.YEARLY_FILES["options_nse"]),
        "BSE": ("options_bse", drive_config.YEARLY_FILES["options_bse"]),
    }
    for exchange, filename in config.OPTIONS_FILENAMES.items():
        _kind_key, pattern = options_targets[exchange]
        items.append(SyncItem(
            kind="options",
            fmt="parquet",
            folder_key="options",
            local_path=directory / filename,
            drive_name=pattern.format(year=year),
            dedup_keys=drive_config.DEDUP_KEYS["options"],
        ))

    # Futures: all indexes -> one per-year file.
    items.append(SyncItem(
        kind="futures",
        fmt="parquet",
        folder_key="futures",
        local_path=directory / config.FUTURES_FILENAME,
        drive_name=drive_config.YEARLY_FILES["futures"].format(year=year),
        dedup_keys=drive_config.DEDUP_KEYS["futures"],
    ))

    # Spot: one cumulative CSV per index.
    for instrument, spec in config.SPOT_UNDERLYINGS.items():
        stem = spec["file_stem"]
        items.append(SyncItem(
            kind="spot",
            fmt="csv",
            folder_key="spot",
            local_path=directory / config.spot_filename(stem),
            drive_name=drive_config.SPOT_FILES[instrument],
            dedup_keys=drive_config.DEDUP_KEYS["spot"],
        ))

    # Only keep items whose per-day file was actually produced.
    present = [i for i in items if i.local_path.exists()]
    missing = [i for i in items if not i.local_path.exists()]
    for item in missing:
        logger.info("No per-day file for %s (%s) — nothing to append.",
                    item.drive_name, item.local_path.name)
    return present


# ============================================================
# PURE APPEND / DEDUP CORE (no Drive, unit-testable)
# ============================================================

def read_frame(path: Path, fmt: str) -> pd.DataFrame:
    """Read a local parquet/csv into a DataFrame."""
    if fmt == "parquet":
        return pd.read_parquet(path, engine="pyarrow")
    return pd.read_csv(path)


def write_frame(df: pd.DataFrame, path: Path, fmt: str):
    """Write a DataFrame to parquet/csv."""
    if fmt == "parquet":
        df.to_parquet(path, engine="pyarrow", index=False, compression="zstd")
    else:
        df.to_csv(path, index=False)


def _sql_literal(value: Path | str) -> str:
    """Return a single-quoted SQL literal for DuckDB."""
    return "'" + str(value).replace("'", "''") + "'"


def _sql_ident(value: str) -> str:
    """Return a double-quoted SQL identifier for DuckDB."""
    return '"' + value.replace('"', '""') + '"'


def parquet_row_count(path: Path) -> int:
    """Count parquet rows without loading the file into pandas."""
    query = f"select count(*) from read_parquet({_sql_literal(path)})"
    with duckdb.connect() as con:
        return con.execute(query).fetchone()[0]


def parquet_columns(path: Path) -> list[str]:
    """Read parquet column names through DuckDB metadata."""
    query = f"describe select * from read_parquet({_sql_literal(path)})"
    with duckdb.connect() as con:
        rows = con.execute(query).fetchall()
    return [row[0] for row in rows]


def parquet_date_count(path: Path, date_iso: str) -> int:
    """Count rows for a trading date without loading parquet into pandas."""
    columns = parquet_columns(path)
    if "Date" in columns:
        predicate = f"{_sql_ident('Date')} = cast({_sql_literal(date_iso)} as date)"
    elif "timestamp" in columns:
        predicate = (
            f"cast({_sql_ident('timestamp')} as date) = "
            f"cast({_sql_literal(date_iso)} as date)"
        )
    else:
        return 0

    query = (
        f"select count(*) from read_parquet({_sql_literal(path)}) "
        f"where {predicate}"
    )
    with duckdb.connect() as con:
        return con.execute(query).fetchone()[0]


def merge_parquet_with_duckdb(existing_path: Path | None, new_day_path: Path,
                              output_path: Path, dedup_keys) -> dict:
    """Merge parquet files with DuckDB and write a deduplicated parquet.

    Existing rows win over new rows on key collisions. DuckDB scans the
    parquet inputs directly, avoiding the large pandas DataFrame copies
    that can make macOS kill the process.
    """
    before = 0 if existing_path is None else parquet_row_count(existing_path)
    new_rows = parquet_row_count(new_day_path)
    if new_rows == 0:
        return {"before": before, "after": before, "added": 0, "empty": True}

    columns = parquet_columns(existing_path or new_day_path)
    keys = [key for key in dedup_keys if key in columns] or columns
    partition_by = ", ".join(_sql_ident(key) for key in keys)

    new_sql = f"select 1 as _source_order, row_number() over () as _row_order, * from read_parquet({_sql_literal(new_day_path)})"
    if existing_path is None:
        source_sql = new_sql
    else:
        existing_sql = f"select 0 as _source_order, row_number() over () as _row_order, * from read_parquet({_sql_literal(existing_path)})"
        source_sql = f"{existing_sql} union all {new_sql}"

    query = f"""
        copy (
            with source as (
                {source_sql}
            ),
            ranked as (
                select
                    * exclude (_source_order, _row_order),
                    row_number() over (
                        partition by {partition_by}
                        order by _source_order, _row_order
                    ) as _dedup_rank
                from source
            )
            select * exclude (_dedup_rank)
            from ranked
            where _dedup_rank = 1
        )
        to {_sql_literal(output_path)}
        (format parquet, compression zstd)
    """
    with duckdb.connect() as con:
        con.execute(query)

    after = parquet_row_count(output_path)
    if after > before + new_rows:
        raise DriveError(
            f"DuckDB merge produced too many rows: before={before}, "
            f"new={new_rows}, after={after}"
        )
    return {"before": before, "after": after, "added": after - before,
            "empty": False, "new_rows": new_rows}


def merge_and_dedup(existing: pd.DataFrame | None, new_day: pd.DataFrame,
                    dedup_keys) -> pd.DataFrame:
    """Concatenate new_day onto existing and drop duplicate rows.

    Existing rows win over new rows on a key collision (keep="first" after
    placing existing first), so a re-run never changes already-stored data.
    dedup_keys present in the frames are used; if some are absent the whole
    row is used as the key (safe fallback).
    """
    if existing is None or existing.empty:
        combined = new_day.copy()
    else:
        combined = pd.concat([existing, new_day], ignore_index=True)

    keys = [k for k in dedup_keys if k in combined.columns]
    if keys:
        combined = combined.drop_duplicates(subset=keys, keep="first")
    else:
        combined = combined.drop_duplicates(keep="first")

    return combined.reset_index(drop=True)


# ============================================================
# SIDECAR (.dates.json) HELPERS
# ============================================================

def sidecar_name(drive_name: str) -> str:
    """Sidecar file name for a per-year file: .<name>.dates.json."""
    return f".{drive_name}.dates.json"


def parse_sidecar(text: str | None):
    """Parse sidecar JSON into a set of ISO date strings."""
    if not text:
        return set()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Sidecar JSON was unreadable — treating as empty.")
        return set()
    if isinstance(data, list):
        return {str(d) for d in data}
    return set()


def dump_sidecar(dates) -> str:
    """Serialize a set of ISO date strings to sorted sidecar JSON."""
    return json.dumps(sorted(dates))


# ============================================================
# PER-ITEM SYNC (Drive)
# ============================================================

def sync_item(drive: DriveClient, item: SyncItem, trading_date: date,
              work_dir: Path, dry_run: bool = False,
              repair_sidecar: bool = False,
              repair_sidecar_only: bool = False) -> dict:
    """Append one item's per-day file into its per-year Drive file."""
    date_iso = trading_date.isoformat()
    folder_id = drive_config.DRIVE_FOLDERS[item.folder_key]

    logger.info("── %s  ->  %s", item.local_path.name, item.drive_name)

    # 1) sidecar idempotency check
    sidecar_file = sidecar_name(item.drive_name)
    sidecar_text = drive.read_text(folder_id, sidecar_file)
    ingested = parse_sidecar(sidecar_text)
    if date_iso in ingested:
        logger.info("   already ingested %s — skipping.", date_iso)
        return {"item": item.drive_name, "status": "already_ingested",
                "date": date_iso}

    # 2) check the new day locally
    if item.fmt == "parquet":
        new_day_rows = parquet_row_count(item.local_path)
        if new_day_rows == 0:
            logger.info("   per-day file is empty — skipping.")
            return {"item": item.drive_name, "status": "empty_source",
                    "date": date_iso}
    else:
        new_day = read_frame(item.local_path, item.fmt)
        new_day_rows = len(new_day)
        if new_day.empty:
            logger.info("   per-day file is empty — skipping.")
            return {"item": item.drive_name, "status": "empty_source",
                    "date": date_iso}

    # 3) resolve / download the current per-year file
    existing_id = drive.find_file(folder_id, item.drive_name)
    existing = None
    downloaded = None
    existing_date_rows = 0
    if existing_id:
        downloaded = work_dir / f"current_{item.drive_name}"
        drive.download_file(existing_id, downloaded)
        if item.fmt == "parquet":
            existing_rows = parquet_row_count(downloaded)
            existing_date_rows = parquet_date_count(downloaded, date_iso)
            logger.info("   current per-year rows: %s", existing_rows)
            if existing_date_rows:
                if existing_date_rows != new_day_rows:
                    raise DriveError(
                        f"{item.drive_name} already contains "
                        f"{existing_date_rows} rows for {date_iso}, but local "
                        f"{item.local_path.name} has {new_day_rows}. Refusing "
                        "to append or repair sidecar until this mismatch is "
                        "checked."
                    )
                logger.warning(
                    "   %s already contains %s rows for %s, but %s is missing "
                    "that date.",
                    item.drive_name, existing_date_rows, date_iso, sidecar_file,
                )
                if repair_sidecar and not dry_run:
                    ingested.add(date_iso)
                    sidecar_path = work_dir / sidecar_file
                    sidecar_path.write_text(dump_sidecar(ingested),
                                            encoding="utf-8")
                    drive.upsert_file(folder_id, sidecar_file, sidecar_path)
                    logger.info("   repaired sidecar %s.", sidecar_file)
                    status = "sidecar_repaired"
                else:
                    logger.info("   not appending; use --repair-sidecar to "
                                "record the existing date.")
                    status = "sidecar_repair_needed"
                return {
                    "item": item.drive_name,
                    "status": status,
                    "date": date_iso,
                    "rows_before": existing_rows,
                    "rows_after": existing_rows,
                    "rows_added": 0,
                    "archived": False,
                }
        else:
            existing = read_frame(downloaded, item.fmt)
            logger.info("   current per-year rows: %s", len(existing))
            if "timestamp" in existing.columns:
                existing_dates = pd.to_datetime(
                    existing["timestamp"], errors="coerce"
                ).dt.date
                existing_date_rows = (existing_dates == trading_date).sum()
            elif "Date" in existing.columns:
                existing_dates = pd.to_datetime(
                    existing["Date"], errors="coerce"
                ).dt.date
                existing_date_rows = (existing_dates == trading_date).sum()
            else:
                existing_date_rows = 0
            if existing_date_rows:
                if existing_date_rows != new_day_rows:
                    raise DriveError(
                        f"{item.drive_name} already contains "
                        f"{existing_date_rows} rows for {date_iso}, but local "
                        f"{item.local_path.name} has {new_day_rows}. Refusing "
                        "to append or repair sidecar until this mismatch is "
                        "checked."
                    )
                logger.warning(
                    "   %s already contains %s rows for %s, but %s is missing "
                    "that date.",
                    item.drive_name, existing_date_rows, date_iso, sidecar_file,
                )
                if repair_sidecar and not dry_run:
                    ingested.add(date_iso)
                    sidecar_path = work_dir / sidecar_file
                    sidecar_path.write_text(dump_sidecar(ingested),
                                            encoding="utf-8")
                    drive.upsert_file(folder_id, sidecar_file, sidecar_path)
                    logger.info("   repaired sidecar %s.", sidecar_file)
                    status = "sidecar_repaired"
                else:
                    logger.info("   not appending; use --repair-sidecar to "
                                "record the existing date.")
                    status = "sidecar_repair_needed"
                return {
                    "item": item.drive_name,
                    "status": status,
                    "date": date_iso,
                    "rows_before": len(existing),
                    "rows_after": len(existing),
                    "rows_added": 0,
                    "archived": False,
                }
    else:
        if repair_sidecar_only:
            logger.info("   per-year file not found — sidecar repair skipped.")
            return {"item": item.drive_name, "status": "missing_target",
                    "date": date_iso}
        logger.info("   per-year file not found — will create it.")

    if repair_sidecar_only:
        logger.info("   no existing rows for %s — sidecar repair skipped.",
                    date_iso)
        return {"item": item.drive_name, "status": "not_present",
                "date": date_iso}

    # 4) merge + dedup, then write locally
    merged_path = work_dir / f"merged_{item.drive_name}"
    if item.fmt == "parquet":
        stats = merge_parquet_with_duckdb(
            downloaded, item.local_path, merged_path, item.dedup_keys
        )
        before = stats["before"]
        rows_after = stats["after"]
        added = stats["added"]
    else:
        before = 0 if existing is None else len(existing)
        combined = merge_and_dedup(existing, new_day, item.dedup_keys)
        rows_after = len(combined)
        added = rows_after - before
        write_frame(combined, merged_path, item.fmt)
        verify = read_frame(merged_path, item.fmt)
        if len(verify) != rows_after:
            raise DriveError(
                f"Read-back mismatch for {item.drive_name}: "
                f"{len(verify)} != {rows_after}"
            )
    logger.info("   rows: %s -> %s (+%s)", before, rows_after, added)

    if dry_run:
        logger.info("   dry run — not uploading %s.", item.drive_name)
        return {
            "item": item.drive_name,
            "status": "dry_run",
            "date": date_iso,
            "rows_before": before,
            "rows_after": rows_after,
            "rows_added": added,
            "archived": False,
        }

    # 5) atomic replace on Drive (old file untouched until this succeeds)
    file_id, created = drive.upsert_file(folder_id, item.drive_name, merged_path)

    # 6) update sidecar
    ingested.add(date_iso)
    sidecar_path = work_dir / sidecar_file
    sidecar_path.write_text(dump_sidecar(ingested), encoding="utf-8")
    drive.upsert_file(folder_id, sidecar_file, sidecar_path)

    # 7) archive the raw per-day file
    archived = False
    if drive_config.archive_configured():
        archive_name = f"{date_iso}_{item.local_path.name}"
        drive.upload_file(
            drive_config.DRIVE_ARCHIVE_FOLDER, archive_name, item.local_path
        )
        archived = True

    logger.info("   %s (file_id=%s, added=%s, archived=%s)",
                "created" if created else "updated", file_id, added, archived)

    return {
        "item": item.drive_name,
        "status": "created" if created else "updated",
        "date": date_iso,
        "rows_before": before,
        "rows_after": rows_after,
        "rows_added": added,
        "archived": archived,
    }


# ============================================================
# ENTRY POINT
# ============================================================

def sync_date(trading_date: date, drive: DriveClient | None = None,
              dry_run: bool = False, repair_sidecar: bool = False,
              repair_sidecar_only: bool = False) -> dict:
    """Append every present per-day file for a trading date to Drive."""
    if not drive_config.folders_configured():
        raise DriveError(
            "Drive folder IDs are not configured. Edit drive_config.py and "
            "set DRIVE_FOLDERS (see DRIVE_SETUP.md)."
        )

    items = build_sync_items(trading_date)
    if not items:
        logger.warning("No per-day files found for %s — nothing to sync.",
                       trading_date)
        return {"date": trading_date.isoformat(), "results": [], "errors": {}}

    drive = drive or DriveClient()
    results = []
    errors = {}

    with tempfile.TemporaryDirectory(prefix="drive_sync_") as tmp:
        work_dir = Path(tmp)
        for item in items:
            try:
                results.append(sync_item(
                    drive, item, trading_date, work_dir, dry_run=dry_run,
                    repair_sidecar=repair_sidecar,
                    repair_sidecar_only=repair_sidecar_only,
                ))
            except DriveError as error:
                logger.error("Failed to sync %s: %s", item.drive_name, error)
                errors[item.drive_name] = str(error)
            except Exception as error:  # noqa: BLE001 - isolate per file
                logger.error("Failed to sync %s: %s", item.drive_name, error)
                errors[item.drive_name] = str(error)

    logger.info("=" * 60)
    logger.info("Drive sync %s: %s ok, %s failed",
                trading_date, len(results), len(errors))
    return {"date": trading_date.isoformat(), "results": results, "errors": errors}


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Append per-day local outputs into the per-year files "
                    "on Google Drive (idempotent, atomic, archived)."
    )
    parser.add_argument(
        "--date",
        help="Trading date YYYY-MM-DD to sync. Defaults to today (IST).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and merge locally, but do not upload or archive.",
    )
    parser.add_argument(
        "--repair-sidecar",
        action="store_true",
        help="If the Drive file already contains the date with the same row "
             "count as the local file, update only the .dates.json sidecar.",
    )
    parser.add_argument(
        "--repair-sidecar-only",
        action="store_true",
        help="Only repair missing .dates.json entries; never append/upload "
             "data files.",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    if args.date:
        trading_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        trading_date = datetime.now(IST).date()

    summary = sync_date(
        trading_date,
        dry_run=args.dry_run,
        repair_sidecar=args.repair_sidecar or args.repair_sidecar_only,
        repair_sidecar_only=args.repair_sidecar_only,
    )

    if summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
