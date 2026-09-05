from datetime import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo


# India market timezone. Declared first because the default
# collection window is derived from the current IST date.
IST = ZoneInfo("Asia/Kolkata")


APP_ID = os.getenv("APP_ID")
APP_TYPE = "100"

# Indexes collected when --underlying is not supplied.
DEFAULT_UNDERLYINGS = [
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
]
UNDERLYINGS = {
    "NIFTY": {
        "symbol": "NSE:NIFTY50-INDEX",
        "exchange": "NSE",
    },
    "BANKNIFTY": {
        "symbol": "NSE:NIFTYBANK-INDEX",
        "exchange": "NSE",
    },
    "SENSEX": {
        "symbol": "BSE:SENSEX-INDEX",
        "exchange": "BSE",
    },
}

# ------------------------------------------------------------
# FUTURES
#
# Index futures are collected for all three indexes across the three
# nearest monthly contracts (near / next / far), tagged with the same
# continuous-symbol convention as the historical archive so daily rows
# append cleanly into INDEX_FUTURES_<year>.parquet:
#
#   NIFTY-I.NFO   BANKNIFTY-I.NFO   SENSEX-I.NFO      (near month)
#   NIFTY-II.NFO  BANKNIFTY-II.NFO  SENSEX-II.NFO     (next month)
#   NIFTY-III.NFO BANKNIFTY-III.NFO SENSEX-III.NFO    (far month)
#
# ContractSeries carries I / II / III. SourceFile marks the origin of
# the row; daily rows produced by this collector use FUTURES_SOURCE_FILE.
# ------------------------------------------------------------

# Underlyings collected for futures. SENSEX is included but a SENSEX
# failure is logged and skipped without aborting the run.
FUTURES_UNDERLYINGS = [
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
]

# Root (undated) FYERS symbol per index used to resolve the active
# monthly futures contracts from the symbol master, and the exchange
# each index's futures trade on.
FUTURES_SYMBOLS = {
    "NIFTY": {"root": "NIFTY", "exchange": "NSE", "segment": "NSE_FO"},
    "BANKNIFTY": {"root": "BANKNIFTY", "exchange": "NSE", "segment": "NSE_FO"},
    "SENSEX": {"root": "SENSEX", "exchange": "BSE", "segment": "BSE_FO"},
}

# Continuous-contract suffixes, in near -> far order. Position maps to
# ContractSeries and to the historical -I / -II / -III ticker suffix.
FUTURES_CONTRACT_SERIES = ["I", "II", "III"]

# Number of nearest monthly contracts collected per index.
FUTURES_CONTRACT_COUNT = len(FUTURES_CONTRACT_SERIES)

# Written into the SourceFile column for every daily futures row so the
# origin of live-collected data is distinguishable from backfilled rows.
FUTURES_SOURCE_FILE = "fyers_daily_extraction"

FUTURES_COLUMNS = [
    "Ticker",
    "Date",
    "Time",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "OpenInterest",
    "SourceFile",
    "Underlying",
    "ContractSeries",
]

# FYERS symbol master (used to discover active futures contracts).
# Large files (>10MB); stream to disk rather than holding in memory.
SYMBOL_MASTER_URLS = {
    "NSE_FO": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "BSE_FO": "https://public.fyers.in/sym_details/BSE_FO.csv",
}

# ------------------------------------------------------------
# SPOT (index underlying)
#
# 1-minute index candles, one CSV per index, matching the historical
# spot files (Nifty 50.csv, Nifty Bank.csv, Sensex.csv). No open
# interest for an index.
# ------------------------------------------------------------

# name       -> the FYERS index symbol and the CSV file stem used both
#               for the daily per-date CSV and the cumulative history.
SPOT_UNDERLYINGS = {
    "NIFTY": {"symbol": "NSE:NIFTY50-INDEX", "file_stem": "Nifty 50"},
    "BANKNIFTY": {"symbol": "NSE:NIFTYBANK-INDEX", "file_stem": "Nifty Bank"},
    "SENSEX": {"symbol": "BSE:SENSEX-INDEX", "file_stem": "Sensex"},
}

SPOT_COLUMNS = [
    "timestamp",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
]

# ------------------------------------------------------------
# DEFAULT COLLECTION WINDOW
#
# Defaults to the current IST trading date so the collector can
# be scheduled daily without editing this file.
#
# Override on the command line for backfills:
#   python production_collector.py --date 2026-08-19
#   python production_collector.py --start-date 2026-08-03 \
#                                  --end-date 2026-08-19
# ------------------------------------------------------------

TODAY_IST = datetime.now(IST).date().isoformat()

START_DATE = TODAY_IST
END_DATE = TODAY_IST

# Strikes fetched either side of ATM, per expiry, per index.
# 20 gives ~82 contracts per expiry (41 strikes x CE/PE), which covers
# any realistic strategy width. Going much higher mostly adds strikes
# that never trade intraday.
STRIKE_COUNT = 40

REQUEST_DELAY_SECONDS = 0.25
MAX_RETRIES = 4
INITIAL_RETRY_DELAY_SECONDS = 1.0

TOKEN_FILE = Path("fyers_access_token.json")
DATA_DIR = Path("data")
LOG_DIR = Path("logs")

EXPECTED_INTRADAY_CANDLES = 385

COLUMNS = [
    "Ticker",
    "Date",
    "Time",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "OpenInterest",
    "Underlying",
    "Expiry",
    "Strike",
    "OptionType",
]

# ------------------------------------------------------------
# PER-DAY OUTPUT LAYOUT
#
# One directory per trading date under DATA_DIR:
#   data/date=YYYY-MM-DD/
#       options_nse.parquet   NIFTY + BANKNIFTY options
#       options_bse.parquet   SENSEX options
#       futures.parquet       all index futures (near/next/far)
#       spot_<stem>.csv        one spot CSV per index
#
# Options are split by the exchange the underlying trades on, so daily
# files append into the matching per-year archive (NSE_* vs BSE_*).
# ------------------------------------------------------------

# underlying -> exchange bucket used to route options into the NSE or
# BSE per-day file (and, later, the matching per-year archive).
OPTIONS_EXCHANGE = {
    "NIFTY": "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX": "BSE",
}

OPTIONS_FILENAMES = {
    "NSE": "options_nse.parquet",
    "BSE": "options_bse.parquet",
}

FUTURES_FILENAME = "futures.parquet"


def spot_filename(file_stem):
    """Per-day spot CSV name for a given index file stem."""
    return f"spot_{file_stem}.csv"
