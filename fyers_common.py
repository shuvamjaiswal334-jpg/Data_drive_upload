"""Shared FYERS primitives used by every collector.

Extracted so the options, futures, and spot collectors can share one
implementation of the client bootstrap, the retry/no-data policy, and the
atomic parquet/CSV writers without importing each other and creating an
import cycle.

Nothing here makes a network call at import time.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from fyers_apiv3 import fyersModel

import config

logger = logging.getLogger(__name__)


IST = config.IST
APP_ID = config.APP_ID
APP_TYPE = config.APP_TYPE
MAX_RETRIES = config.MAX_RETRIES
INITIAL_RETRY_DELAY = config.INITIAL_RETRY_DELAY_SECONDS


# ============================================================
# FYERS CLIENT
# ============================================================

def create_fyers_client(token_file: Path | None = None):
    """Build a FyersModel from the saved daily token.

    Warns (does not fail) when the token was not saved today, because
    FYERS tokens expire daily and a stale token surfaces as auth errors
    on the first real request.
    """
    token_file = Path(token_file) if token_file else config.TOKEN_FILE

    if not token_file.exists():
        raise FileNotFoundError(
            f"Token file not found: {token_file}\n"
            "Run 'python login.py' first to authenticate."
        )

    with token_file.open("r", encoding="utf-8") as handle:
        token_data = json.load(handle)

    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError(
            f"access_token missing from {token_file}\n"
            "Run 'python login.py' to generate a fresh token."
        )

    token_date = datetime.fromtimestamp(token_file.stat().st_mtime, tz=IST).date()
    today_ist = datetime.now(IST).date()
    if token_date != today_ist:
        logger.warning(
            "Token saved on %s (today is %s IST). FYERS tokens expire "
            "daily — run 'python login.py' if requests fail.",
            token_date, today_ist,
        )

    client_id = f"{APP_ID}-{APP_TYPE}"
    return fyersModel.FyersModel(
        client_id=client_id,
        token=access_token,
        is_async=False,
        log_path="",
    )


# ============================================================
# RETRY / NO-DATA POLICY
# ============================================================

class NoDataAvailable(RuntimeError):
    """FYERS gave a definitive "no data" answer for this request.

    Not a fault and must not be retried: the contract simply did not
    trade in the requested window. Retrying only burns API calls and
    backoff for an answer that will not change.
    """


def is_no_data_response(response) -> bool:
    """True if FYERS says this request had no trades.

    Dead instruments come back as:
        {'candles': [], 's': 'no_data', 'message': '', 'code': 200}
    The signal is s='no_data'; message is usually blank.
    """
    if not isinstance(response, dict):
        return False

    status = str(response.get("s", "")).lower()
    message = str(response.get("message", "")).lower()

    if status == "no_data":
        return True
    if "no data" in message or "no candle" in message:
        return True
    return False


def call_with_retry(function, description, require_ok=True):
    """Call a FYERS function with bounded retry and exponential backoff.

    A definitive no-data answer raises NoDataAvailable immediately and is
    never retried. Any other non-ok response or exception is retried up to
    MAX_RETRIES with 1s/2s/4s backoff.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = function()
            if (
                require_ok
                and isinstance(response, dict)
                and response.get("s") != "ok"
            ):
                if is_no_data_response(response):
                    raise NoDataAvailable(response.get("message") or "no data")
                raise RuntimeError(response)
            return response

        except NoDataAvailable:
            raise

        except Exception as error:  # noqa: BLE001 - retried below
            last_error = error
            logger.warning(
                "%s failed (attempt %s/%s): %s",
                description, attempt, MAX_RETRIES, error,
            )
            if attempt < MAX_RETRIES:
                delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
                logger.info("Retrying in %.1f seconds...", delay)
                time.sleep(delay)

    raise RuntimeError(
        f"{description} failed after {MAX_RETRIES} attempts: {last_error}"
    )


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_datetime(epoch_seconds) -> datetime:
    """Convert a FYERS candle epoch to an IST-aware datetime."""
    return datetime.fromtimestamp(epoch_seconds, tz=IST)


# ============================================================
# ATOMIC WRITERS
# ============================================================

def atomic_write_parquet(df: pd.DataFrame, output_file: Path, *, overwrite: bool = False):
    """Write a DataFrame to Parquet atomically with a read-back check.

    Data is written to a temp file, read back, row-count verified, then
    renamed into place. An interrupted write can never leave a partial
    file at output_file. Refuses to clobber existing data unless
    overwrite=True.
    """
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite: {output_file}")

    temp_file = output_file.with_suffix(output_file.suffix + ".tmp")
    df.to_parquet(temp_file, engine="pyarrow", index=False, compression="zstd")

    try:
        read_back = pd.read_parquet(temp_file, engine="pyarrow")
        if len(read_back) != len(df):
            raise RuntimeError("Read-back row count mismatch.")
        temp_file.replace(output_file)
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise

    return output_file


def atomic_write_csv(df: pd.DataFrame, output_file: Path, *, overwrite: bool = False):
    """Write a DataFrame to CSV atomically with a read-back check."""
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite: {output_file}")

    temp_file = output_file.with_suffix(output_file.suffix + ".tmp")
    df.to_csv(temp_file, index=False)

    try:
        read_back = pd.read_csv(temp_file)
        if len(read_back) != len(df):
            raise RuntimeError("Read-back row count mismatch.")
        temp_file.replace(output_file)
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise

    return output_file
