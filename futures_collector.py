"""Index futures collection: 1-minute candles for the active monthly
contracts (near / next / far) of NIFTY, BANKNIFTY, and SENSEX.

Output rows match the historical archive's 12-column futures schema
(config.FUTURES_COLUMNS), tagged with:

  * the continuous archive Ticker (e.g. NIFTY-I.NFO)
  * ContractSeries (I / II / III)
  * SourceFile = config.FUTURES_SOURCE_FILE, marking these as live daily
    extraction rather than backfill.

History is downloaded with the shared retry/no-data policy so a contract
that did not trade is skipped, not retried. SENSEX is included but any
SENSEX-specific failure is isolated by the symbol resolver and by
per-contract error handling, so it never aborts the run.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd

import config
import futures_symbols
from fyers_common import (
    NoDataAvailable,
    call_with_retry,
    candle_datetime,
    atomic_write_parquet,
)

logger = logging.getLogger(__name__)


REQUEST_DELAY_SECONDS = config.REQUEST_DELAY_SECONDS
FUTURES_COLUMNS = config.FUTURES_COLUMNS
FUTURES_SOURCE_FILE = config.FUTURES_SOURCE_FILE


def download_futures_contract(fyers, contract: dict, trading_date: date):
    """Download 1-minute candles for a single futures contract.

    Returns a list of row dicts in FUTURES_COLUMNS order. Raises
    NoDataAvailable if the contract did not trade that day.
    """
    symbol = contract["symbol"]

    request = {
        "symbol": symbol,
        "resolution": "1",
        "date_format": "1",
        "range_from": trading_date.strftime("%Y-%m-%d"),
        "range_to": trading_date.strftime("%Y-%m-%d"),
        "cont_flag": "1",
        "oi_flag": "1",
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
        if len(candle) < 7:
            raise RuntimeError(f"Unexpected candle structure: {candle}")

        dt = candle_datetime(candle[0])

        rows.append({
            "Ticker": contract["ticker"],
            "Date": dt.date(),
            "Time": dt.strftime("%H:%M:%S"),
            "Open": candle[1],
            "High": candle[2],
            "Low": candle[3],
            "Close": candle[4],
            "Volume": candle[5],
            "OpenInterest": candle[6],
            "SourceFile": FUTURES_SOURCE_FILE,
            "Underlying": contract["underlying"],
            "ContractSeries": contract["contract_series"],
        })

    return rows


def build_futures_dataframe(rows) -> pd.DataFrame:
    """Assemble collected rows into a typed, sorted futures DataFrame."""
    df = pd.DataFrame(rows, columns=FUTURES_COLUMNS)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date

    for col in ["Open", "High", "Low", "Close", "Volume", "OpenInterest"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values(
        ["Underlying", "ContractSeries", "Time"]
    ).reset_index(drop=True)
    return df


def collect_futures(fyers, trading_date: date, output_file):
    """Collect all index futures for one trading date and save one file.

    Writes config.FUTURES_FILENAME (a Parquet) under the date directory.
    Returns a summary dict. Never raises for a single failed contract or
    a skipped index; only raises if nothing at all could be collected.
    """
    logger.info("-" * 60)
    logger.info("Collecting FUTURES for %s", trading_date)

    contracts, skipped = futures_symbols.resolve_all_futures(trading_date)

    for instrument, reason in skipped.items():
        logger.warning("Futures index skipped: %s (%s)", instrument, reason)

    if not contracts:
        raise RuntimeError(
            f"No futures contracts resolved for {trading_date}."
        )

    all_rows = []
    total = len(contracts)
    successful = 0
    no_data = 0
    failed = 0

    for number, contract in enumerate(contracts, start=1):
        logger.info(
            "  [%s/%s] %s (%s, series=%s, expiry=%s)",
            number, total, contract["symbol"], contract["ticker"],
            contract["contract_series"], contract["expiry"],
        )

        try:
            rows = download_futures_contract(fyers, contract, trading_date)
            all_rows.extend(rows)
            successful += 1
            logger.info("    Candles: %s", len(rows))

        except NoDataAvailable as no_data_error:
            no_data += 1
            logger.info("    No trades: %s", no_data_error)

        except Exception as error:  # noqa: BLE001 - one bad contract must not stop the run
            failed += 1
            logger.error("    FAILED: %s", error)

        time.sleep(REQUEST_DELAY_SECONDS)

    if not all_rows:
        raise RuntimeError(
            f"No futures data downloaded on {trading_date}."
        )

    df = build_futures_dataframe(all_rows)
    atomic_write_parquet(df, output_file)

    logger.info(
        "Futures saved: %s (%s rows, %s contracts with data, "
        "%s no trades, %s failed, %s indexes skipped)",
        output_file, len(df), successful, no_data, failed, len(skipped),
    )

    return {
        "type": "futures",
        "contracts": total,
        "successful": successful,
        "no_data": no_data,
        "failed": failed,
        "skipped_indexes": list(skipped.keys()),
        "rows": len(df),
        "output": str(output_file),
    }
