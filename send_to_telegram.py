"""Send a trading day's collected files to Telegram.

Sends every per-day artifact produced under data/date=YYYY-MM-DD/ as a
separate document, each with a caption stating the date and what the file
contains (row count, indexes, and for futures/spot the tickers). Files
that were not produced for the day are skipped without error.

Env (from .env locally or GitHub secrets in CI):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

import config

load_dotenv()


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IST = config.IST

# Human labels for each expected per-day file.
FILE_LABELS = {
    config.OPTIONS_FILENAMES["NSE"]: "NSE Index Options (NIFTY, BANKNIFTY)",
    config.OPTIONS_FILENAMES["BSE"]: "BSE Index Options (SENSEX)",
    config.FUTURES_FILENAME: "Index Futures (near/next/far)",
}


def _date_dir(trading_date):
    return config.DATA_DIR / f"date={trading_date.isoformat()}"


def expected_files():
    """Ordered list of expected per-day file names."""
    names = [
        config.OPTIONS_FILENAMES["NSE"],
        config.OPTIONS_FILENAMES["BSE"],
        config.FUTURES_FILENAME,
    ]
    for spec in config.SPOT_UNDERLYINGS.values():
        names.append(config.spot_filename(spec["file_stem"]))
    return names


def summarize(path: Path) -> str:
    """Build a short human summary of a file's contents for the caption."""
    try:
        if path.suffix == ".parquet":
            df = pd.read_parquet(path, engine="pyarrow")
        else:
            df = pd.read_csv(path)
    except Exception as error:  # noqa: BLE001 - caption is best-effort
        return f"{path.name} (could not read: {error})"

    rows = len(df)
    parts = [f"{rows:,} rows"]

    if "Underlying" in df.columns:
        indexes = ", ".join(sorted(str(u) for u in df["Underlying"].unique()))
        parts.append(f"indexes: {indexes}")
    if "Ticker" in df.columns:
        parts.append(f"{df['Ticker'].nunique()} contracts")

    return " | ".join(parts)


def caption_for(path: Path, extraction_date: str) -> str:
    label = FILE_LABELS.get(path.name)
    if label is None:
        # Spot files: label by index stem from the filename.
        label = f"Spot: {path.stem.replace('spot_', '')}"

    return (
        "✅ Fyers data extraction\n\n"
        f"📅 Date: {extraction_date}\n"
        f"📁 {label}\n"
        f"🗂 File: {path.name}\n"
        f"📊 {summarize(path)}"
    )


def send_document(path: Path, extraction_date: str):
    """Send one file to Telegram with a descriptive caption."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    with path.open("rb") as handle:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": caption_for(path, extraction_date),
            },
            files={"document": (path.name, handle)},
            timeout=300,
        )
    response.raise_for_status()
    print(f"Sent: {path.name}")


def send_day(trading_date):
    """Send all present per-day files for a trading date."""
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
    if not CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID is not configured")

    extraction_date = trading_date.isoformat()
    directory = _date_dir(trading_date)

    if not directory.exists():
        raise FileNotFoundError(f"No data directory for {extraction_date}: {directory}")

    sent = 0
    skipped = []
    for name in expected_files():
        path = directory / name
        if not path.exists():
            skipped.append(name)
            continue
        send_document(path, extraction_date)
        sent += 1

    print(f"\nTelegram: sent {sent} file(s) for {extraction_date}.")
    if skipped:
        print(f"Skipped (not produced): {', '.join(skipped)}")

    if sent == 0:
        raise FileNotFoundError(
            f"No expected files found to send in {directory}."
        )

    return {"date": extraction_date, "sent": sent, "skipped": skipped}


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Send a trading day's collected files to Telegram, each "
                    "captioned with the date and contents."
    )
    parser.add_argument(
        "--date",
        help="Trading date YYYY-MM-DD. Defaults to today (IST).",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    if args.date:
        trading_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        trading_date = datetime.now(IST).date()

    send_day(trading_date)


if __name__ == "__main__":
    main()
