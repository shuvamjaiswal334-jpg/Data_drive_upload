"""Spot (index underlying) collection: 1-minute candles for NIFTY,
BANKNIFTY, and SENSEX indexes.

Output matches the historical spot files (Nifty 50.csv, Nifty Bank.csv,
Sensex.csv): the 6-column config.SPOT_COLUMNS schema
(timestamp, Open, High, Low, Close, Volume). An index has no open
interest, so OI is not collected.

One CSV is written per index for the trading date. SENSEX is included but
any SENSEX-specific failure is isolated per index, so it never aborts the
run for the NSE indexes.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd

import config
from fyers_common import (
    NoDataAvailable,
    call_with_retry,
    candle_datetime,
    atomic_write_csv,
)

logger = logging.getLogger(__name__)


REQUEST_DELAY_SECONDS = config.REQUEST_DELAY_SECONDS
SPOT_COLUMNS = config.SPOT_COLUMNS
SPOT_UNDERLYINGS = config.SPOT_UNDERLYINGS

# Full IST datetime, so rows stay unique across days in the cumulative
# history file (a bare HH:MM:SS would collide every trading day).
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def download_spot_index(fyers, symbol: str, trading_date: date):
    """Download 1-minute index candles for one index symbol.

    Returns a list of row dicts in SPOT_COLUMNS order. Raises
    NoDataAvailable if the index returned no candles (e.g. a non-trading
    day that slipped past the holiday filter).
    """
    request = {
        "symbol": symbol,
        "resolution": "1",
        "date_format": "1",
        "range_from": trading_date.strftime("%Y-%m-%d"),
        "range_to": trading_date.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }

    response = call_with_retry(
        lambda: fyers.history(data=request),
        f"History {symbol}",
    )

    candles = response.get("candles", [])
    if not candles:
        raise NoDataAvailable(f"no candles for {symbol}")

    rows = []
    for candle in candles:
        if len(candle) < 6:
            raise RuntimeError(f"Unexpected candle structure: {candle}")

        dt = candle_datetime(candle[0])

        rows.append({
            "timestamp": dt.strftime(_TIMESTAMP_FORMAT),
            "Open": candle[1],
            "High": candle[2],
            "Low": candle[3],
            "Close": candle[4],
            "Volume": candle[5],
        })

    return rows


def build_spot_dataframe(rows) -> pd.DataFrame:
    """Assemble collected candles into a typed, time-ordered spot frame."""
    df = pd.DataFrame(rows, columns=SPOT_COLUMNS)

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = (
        df.sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="first")
        .reset_index(drop=True)
    )
    return df


def collect_spot(fyers, trading_date: date, output_dir):
    """Collect spot candles for all indexes and save one CSV per index.

    Files are named config.spot_filename(<file_stem>) under output_dir,
    e.g. spot_Nifty 50.csv. Returns a summary dict. A single index
    failure is logged and skipped; only raises if no index produced data.
    """
    output_dir = Path(output_dir)
    logger.info("-" * 60)
    logger.info("Collecting SPOT for %s", trading_date)

    per_index = {}
    successful = 0
    no_data = 0
    failed = 0
    outputs = {}

    for instrument, spec in SPOT_UNDERLYINGS.items():
        symbol = spec["symbol"]
        file_stem = spec["file_stem"]

        logger.info("  %s (%s)", instrument, symbol)

        try:
            rows = download_spot_index(fyers, symbol, trading_date)
            df = build_spot_dataframe(rows)

            output_file = output_dir / config.spot_filename(file_stem)
            atomic_write_csv(df, output_file)

            per_index[instrument] = len(df)
            outputs[instrument] = str(output_file)
            successful += 1
            logger.info("    Candles: %s -> %s", len(df), output_file)

        except NoDataAvailable as no_data_error:
            no_data += 1
            logger.info("    No data: %s", no_data_error)

        except Exception as error:  # noqa: BLE001 - one index must not stop the rest
            failed += 1
            logger.error("    FAILED %s: %s", instrument, error)

        time.sleep(REQUEST_DELAY_SECONDS)

    if successful == 0:
        raise RuntimeError(
            f"No spot data downloaded for any index on {trading_date}."
        )

    logger.info(
        "Spot saved: %s indexes with data, %s no data, %s failed | %s",
        successful, no_data, failed,
        ", ".join(f"{k}={v}" for k, v in per_index.items()),
    )

    return {
        "type": "spot",
        "indexes": len(SPOT_UNDERLYINGS),
        "successful": successful,
        "no_data": no_data,
        "failed": failed,
        "rows_per_index": per_index,
        "rows": sum(per_index.values()),
        "outputs": outputs,
    }
