"""Multi-instrument, multi-expiry FYERS historical options collector.

Downloads 1-minute OHLCV + OI candles for ALL active option contracts
across NIFTY, BANKNIFTY, and SENSEX (all expiries: current week,
next week, monthly) and saves them in a single Parquet file per
trading date:

    data/date=YYYY-MM-DD/options.parquet

Usage:
    python production_collector.py                     # today
    python production_collector.py --date 2026-08-21   # specific day
    python production_collector.py --start-date 2026-08-18 --end-date 2026-08-22
"""

import argparse
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config
import futures_collector
import spot_collector
from fyers_common import (
    NoDataAvailable,
    call_with_retry,
    create_fyers_client,
    candle_datetime,
    atomic_write_parquet,
)
from nse_holidays import is_nse_holiday, get_holiday_name


# ============================================================
# CONFIGURATION
# ============================================================

UNDERLYINGS = config.UNDERLYINGS
DEFAULT_UNDERLYINGS = config.DEFAULT_UNDERLYINGS

START_DATE = config.START_DATE
END_DATE = config.END_DATE

STRIKE_COUNT = config.STRIKE_COUNT
REQUEST_DELAY_SECONDS = config.REQUEST_DELAY_SECONDS

TOKEN_FILE = config.TOKEN_FILE
DATA_DIR = config.DATA_DIR
LOG_DIR = config.LOG_DIR

IST = config.IST
COLUMNS = config.COLUMNS

# Retry policy, client bootstrap, and atomic writers now live in
# fyers_common; MAX_RETRIES / INITIAL_RETRY_DELAY are consumed there.


# ============================================================
# LOGGING
# ============================================================

LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "production_collector.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
    force=True,
)
logger = logging.getLogger(__name__)


# NOTE: The FYERS client, retry policy (call_with_retry / NoDataAvailable),
# IST candle conversion, and atomic writers now live in fyers_common so the
# options, futures, and spot collectors share one implementation.


# ============================================================
# EXPIRY HELPERS
# ============================================================

def parse_expiry_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%d-%m-%Y").date()
    except ValueError:
        return None


def get_expiry_targets(expiry_data, trading_date, max_days_ahead=35):
    """Return [(expiry_date, epoch_string)] for expiries in range.

    FYERS gives us both the human date ("25-08-2026") and the epoch
    ("1787652600"). The epoch is what the optionchain API accepts as
    its `timestamp` parameter to select a specific expiry.

    Default window is 5 weeks: current week + next 4 weeklies + the
    monthly. Anything further out has near-zero intraday liquidity.
    """
    targets = {}

    for item in expiry_data:
        expiry_date = parse_expiry_date(item.get("date"))
        if expiry_date is None:
            continue
        if expiry_date < trading_date:
            continue
        if (expiry_date - trading_date).days > max_days_ahead:
            continue

        epoch = item.get("expiry")
        if epoch in (None, ""):
            continue

        targets[expiry_date] = str(epoch)

    if not targets:
        raise RuntimeError(
            f"No expiries within {max_days_ahead} days of {trading_date} "
            "returned by FYERS."
        )

    return [(d, targets[d]) for d in sorted(targets)]


# ============================================================
# OPTION CONTRACT DISCOVERY (ALL EXPIRIES)
# ============================================================

def discover_contracts(fyers, instrument, trading_date):
    """Discover option contracts across ALL active expiries.

    FYERS optionchain returns contracts for only ONE expiry per call.
    So we call it once with timestamp="" purely to read the list of
    available expiries, then call it again once per expiry using that
    expiry's epoch as the timestamp.
    """

    instrument_config = UNDERLYINGS.get(instrument)
    if not instrument_config:
        raise ValueError(f"Unknown underlying: {instrument}")

    option_chain_symbol = instrument_config["symbol"]

    logger.info("Requesting option chain for %s...", instrument)

    # First call: read the available expiry list.
    request = {
        "symbol": option_chain_symbol,
        "strikecount": STRIKE_COUNT,
        "timestamp": "",
    }

    response = call_with_retry(
        lambda: fyers.optionchain(data=request),
        f"Option-chain {instrument}",
    )

    data = response.get("data", {})
    expiry_data = data.get("expiryData", [])

    logger.info(
        "%s: %s expiries listed by FYERS",
        instrument, len(expiry_data),
    )

    # Expiries to collect, each with the epoch the API needs
    targets = get_expiry_targets(expiry_data, trading_date)
    logger.info(
        "%s: collecting %s expiries: %s",
        instrument, len(targets),
        ", ".join(str(d) for d, _ in targets),
    )

    # Build prefix filter
    if instrument == "BANKNIFTY":
        expected_prefixes = ["NSE:BANKNIFTY", "BANKNIFTY"]
    elif instrument == "NIFTY":
        expected_prefixes = ["NSE:NIFTY", "NIFTY"]
    elif instrument == "SENSEX":
        expected_prefixes = ["BSE:SENSEX", "SENSEX"]
    else:
        expected_prefixes = []

    contracts = {}

    # ------------------------------------------------------------
    # One optionchain call per expiry.
    #
    # Contracts are tagged with the expiry WE REQUESTED rather than
    # a field read back off each option. The option payload does not
    # reliably carry its own expiry, and trusting it caused every
    # contract to be labelled with the nearest expiry.
    # ------------------------------------------------------------

    for expiry_date, expiry_epoch in targets:

        logger.info(
            "%s: fetching expiry %s (epoch %s)...",
            instrument, expiry_date, expiry_epoch,
        )

        exp_request = {
            "symbol": option_chain_symbol,
            "strikecount": STRIKE_COUNT,
            "timestamp": expiry_epoch,
        }

        try:
            exp_response = fyers.optionchain(data=exp_request)
        except Exception as error:
            logger.warning(
                "%s: expiry %s request raised: %s",
                instrument, expiry_date, error,
            )
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not isinstance(exp_response, dict) or exp_response.get("s") != "ok":
            logger.warning(
                "%s: expiry %s rejected by FYERS: %s",
                instrument, expiry_date,
                exp_response.get("message", exp_response)
                if isinstance(exp_response, dict) else exp_response,
            )
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        exp_options = (
            exp_response.get("data", {}).get("optionsChain", [])
        )

        added = 0

        for option in exp_options:
            symbol = option.get("symbol")
            option_type = option.get("option_type")
            strike_price = option.get("strike_price")

            if not symbol:
                continue
            if option_type not in ("CE", "PE"):
                continue
            if strike_price in (None, -1):
                continue

            try:
                strike = int(strike_price)
            except (TypeError, ValueError):
                continue

            # Prefix filter keeps a NIFTY query from picking up BANKNIFTY
            symbol_upper = symbol.upper()
            if expected_prefixes:
                if not any(
                    symbol_upper.startswith(p) for p in expected_prefixes
                ):
                    continue

            contracts[symbol] = {
                "Ticker": symbol,
                "Underlying": instrument,
                "Expiry": expiry_date,
                "Strike": strike,
                "OptionType": option_type,
            }
            added += 1

        logger.info(
            "%s: expiry %s gave %s usable contracts "
            "(%s raw rows returned)",
            instrument, expiry_date, added, len(exp_options),
        )

        time.sleep(REQUEST_DELAY_SECONDS)

    contracts = list(contracts.values())

    if not contracts:
        raise RuntimeError(
            f"No CE/PE contracts discovered for {instrument}."
        )

    expiry_counts = {}
    for c in contracts:
        e = str(c["Expiry"])
        expiry_counts[e] = expiry_counts.get(e, 0) + 1

    logger.info(
        "%s: %s total contracts | per-expiry: %s",
        instrument, len(contracts),
        ", ".join(f"{k}={v}" for k, v in sorted(expiry_counts.items())),
    )

    return contracts


# ============================================================
# CHECK IF DATE ALREADY COLLECTED
# ============================================================

def date_dir(trading_date):
    """Per-date output directory: data/date=YYYY-MM-DD/."""
    return DATA_DIR / f"date={trading_date.isoformat()}"


def _valid_options_file(path):
    """True if an existing options Parquet is present and schema-valid."""
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path, engine="pyarrow")
    except Exception as error:  # noqa: BLE001
        logger.warning("Existing %s is corrupt (%s) — will re-collect.", path, error)
        return False
    if df.empty or list(df.columns) != COLUMNS:
        logger.warning("Existing %s has bad shape — will re-collect.", path)
        return False
    return True


def options_already_collected(trading_date):
    """True only if every expected options split file already exists validly.

    With options split by exchange, a day is "done" for options only when
    both the NSE and BSE files are present and pass a schema check. A
    partial result (e.g. NSE saved, BSE missing) is re-collected.
    """
    directory = date_dir(trading_date)
    for filename in config.OPTIONS_FILENAMES.values():
        if not _valid_options_file(directory / filename):
            return False
    return True


# ============================================================
# DOWNLOAD ONE CONTRACT
# ============================================================

def download_contract(fyers, contract, trading_date):
    symbol = contract["Ticker"]

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
        # s=ok but empty: the contract exists and simply did not trade.
        raise NoDataAvailable(f"no candles for {symbol}")

    rows = []
    for candle in candles:
        if len(candle) < 7:
            raise RuntimeError(f"Unexpected candle structure: {candle}")

        dt = candle_datetime(candle[0])

        rows.append({
            "Ticker": symbol,
            "Date": dt.date(),
            "Time": dt.strftime("%H:%M:%S"),
            "Open": candle[1],
            "High": candle[2],
            "Low": candle[3],
            "Close": candle[4],
            "Volume": candle[5],
            "OpenInterest": candle[6],
            "Underlying": contract["Underlying"],
            "Expiry": contract["Expiry"],
            "Strike": contract["Strike"],
            "OptionType": contract["OptionType"],
        })

    return rows


# ============================================================
# DATA VALIDATION
# ============================================================

def validate_dataframe(df):
    """Validate the combined daily DataFrame."""
    errors = []

    if list(df.columns) != COLUMNS:
        errors.append("Unexpected column structure.")

    if df.empty:
        errors.append("DataFrame is empty.")
        return False, errors

    # Missing values
    for col in COLUMNS:
        if df[col].isna().any():
            errors.append(f"Missing values in {col}.")

    # Option type
    bad_types = set(df["OptionType"].dropna().unique()) - {"CE", "PE"}
    if bad_types:
        errors.append(f"Invalid OptionType: {bad_types}")

    # Duplicates
    dupes = df.duplicated(subset=["Ticker", "Date", "Time"]).sum()
    if dupes:
        errors.append(f"Duplicate rows: {dupes}")

    # OHLC sanity
    invalid_ohlc = (
        (df["High"] < df["Low"])
        | (df["High"] < df["Open"])
        | (df["High"] < df["Close"])
        | (df["Low"] > df["Open"])
        | (df["Low"] > df["Close"])
    ).sum()
    if invalid_ohlc:
        errors.append(f"Invalid OHLC rows: {invalid_ohlc}")

    # Strike
    if (df["Strike"] <= 0).any():
        errors.append("Non-positive strike price.")

    return len(errors) == 0, errors


# ============================================================
# SAVE COMBINED DAILY FILE
# ============================================================

def save_options_split(df, trading_date):
    """Save options split by exchange into the per-date directory.

    NSE underlyings (NIFTY, BANKNIFTY) -> options_nse.parquet
    BSE underlyings (SENSEX)           -> options_bse.parquet

    Each file carries the same 13-column schema and is written atomically
    (temp -> read-back -> validate -> rename). Returns {exchange: path}.
    An exchange with no rows for the day is simply not written.
    """
    directory = date_dir(trading_date)
    directory.mkdir(parents=True, exist_ok=True)

    outputs = {}

    for exchange, filename in config.OPTIONS_FILENAMES.items():
        underlyings = [
            u for u, ex in config.OPTIONS_EXCHANGE.items() if ex == exchange
        ]
        subset = df[df["Underlying"].isin(underlyings)].reset_index(drop=True)

        if subset.empty:
            logger.info(
                "No %s options rows for %s — skipping %s.",
                exchange, trading_date, filename,
            )
            continue

        valid, errors = validate_dataframe(subset)
        if not valid:
            raise RuntimeError(
                f"{exchange} options validation failed: " + "; ".join(errors)
            )

        output_file = atomic_write_parquet(subset, directory / filename)
        outputs[exchange] = str(output_file)
        logger.info(
            "Saved %s options: %s (%s rows)",
            exchange, output_file, len(subset),
        )

    if not outputs:
        raise RuntimeError(
            f"No options rows to save for any exchange on {trading_date}."
        )

    return outputs


# ============================================================
# PROCESS ONE TRADING DATE
# ============================================================

def collect_options(fyers, trading_date, selected_underlyings):
    """Discover and download options for the selected indexes.

    Returns a summary dict. Saves the exchange-split per-day files as a
    side effect. Skips cleanly (status=already_collected) when both split
    files already exist and validate. Raises only if collection was
    attempted and produced no data at all.
    """

    if options_already_collected(trading_date):
        directory = date_dir(trading_date)
        logger.info(
            "SKIPPING options for %s — split files already exist.",
            trading_date,
        )
        return {
            "type": "options",
            "status": "already_collected",
            "contracts": 0,
            "successful": 0,
            "no_data": 0,
            "failed": 0,
            "rows": 0,
            "outputs": {
                ex: str(directory / fn)
                for ex, fn in config.OPTIONS_FILENAMES.items()
            },
        }

    # Discover and download for each index
    all_rows = []
    total_contracts = 0
    total_successful = 0
    total_failed = 0
    total_no_data = 0

    for instrument in selected_underlyings:

        logger.info("-" * 60)
        logger.info("Discovering %s...", instrument)

        try:
            contracts = discover_contracts(fyers, instrument, trading_date)
        except Exception as e:
            logger.error("FAILED to discover %s: %s", instrument, e)
            total_failed += 1
            continue

        total_contracts += len(contracts)

        for number, contract in enumerate(contracts, start=1):
            symbol = contract["Ticker"]
            logger.info(
                "  [%s/%s] %s (expiry=%s)",
                number, len(contracts), symbol, contract["Expiry"],
            )

            try:
                rows = download_contract(fyers, contract, trading_date)
                all_rows.extend(rows)
                total_successful += 1
                logger.info("    Candles: %s", len(rows))

            except NoDataAvailable as no_data:
                # Contract did not trade. Expected for illiquid strikes
                # and far-dated expiries; not a collection fault.
                total_no_data += 1
                logger.info("    No trades: %s", no_data)

            except Exception as error:
                total_failed += 1
                logger.error("    FAILED: %s", error)

            time.sleep(REQUEST_DELAY_SECONDS)

    if not all_rows:
        raise RuntimeError(
            f"No options data downloaded for any index on {trading_date}."
        )

    # Build combined DataFrame
    df = pd.DataFrame(all_rows, columns=COLUMNS)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df["Expiry"] = pd.to_datetime(df["Expiry"]).dt.date

    for col in ["Open", "High", "Low", "Close", "Volume", "OpenInterest", "Strike"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values(
        ["Underlying", "Expiry", "Strike", "OptionType", "Time"]
    ).reset_index(drop=True)

    # Validate the combined frame before the exchange split.
    valid, errors = validate_dataframe(df)
    if not valid:
        for err in errors:
            logger.error("VALIDATION ERROR: %s", err)
        raise RuntimeError("Options data validation failed.")

    underlyings = sorted(df["Underlying"].unique())
    expiries = sorted(str(e) for e in df["Expiry"].unique())
    logger.info(
        "Options collected: %s rows | %s unique contracts | "
        "indexes=%s | expiries=%s",
        len(df), df["Ticker"].nunique(), underlyings, expiries,
    )
    logger.info(
        "Options contracts: %s discovered | %s with data | "
        "%s no trades | %s failed",
        total_contracts, total_successful, total_no_data, total_failed,
    )

    # Save split by exchange (NSE / BSE).
    outputs = save_options_split(df, trading_date)

    return {
        "type": "options",
        "status": "downloaded",
        "contracts": total_contracts,
        "successful": total_successful,
        "no_data": total_no_data,
        "failed": total_failed,
        "rows": len(df),
        "outputs": outputs,
    }


# ============================================================
# PROCESS ONE TRADING DATE (options + futures + spot)
# ============================================================

def process_date(fyers, trading_date, selected_underlyings):
    """Collect options, futures, and spot for one trading date.

    Each data type is isolated: a failure in one (for example SENSEX
    futures or a BSE outage) is recorded and does not prevent the others
    from being collected. The date is only marked "failed" if every data
    type failed.
    """

    logger.info("=" * 80)
    logger.info("Processing: %s", trading_date)
    logger.info("=" * 80)

    result = {
        "date": trading_date,
        "options": None,
        "futures": None,
        "spot": None,
        "errors": {},
    }

    # ---- Options -------------------------------------------------
    try:
        result["options"] = collect_options(
            fyers, trading_date, selected_underlyings
        )
    except Exception as error:  # noqa: BLE001 - isolate this data type
        logger.exception("Options collection failed for %s", trading_date)
        result["errors"]["options"] = str(error)

    # ---- Futures -------------------------------------------------
    try:
        futures_output = date_dir(trading_date) / config.FUTURES_FILENAME
        if futures_output.exists():
            logger.info(
                "SKIPPING futures for %s — %s already exists.",
                trading_date, futures_output,
            )
            result["futures"] = {
                "type": "futures",
                "status": "already_collected",
                "output": str(futures_output),
            }
        else:
            result["futures"] = futures_collector.collect_futures(
                fyers, trading_date, futures_output
            )
    except Exception as error:  # noqa: BLE001 - isolate this data type
        logger.exception("Futures collection failed for %s", trading_date)
        result["errors"]["futures"] = str(error)

    # ---- Spot ----------------------------------------------------
    try:
        result["spot"] = spot_collector.collect_spot(
            fyers, trading_date, date_dir(trading_date)
        )
    except Exception as error:  # noqa: BLE001 - isolate this data type
        logger.exception("Spot collection failed for %s", trading_date)
        result["errors"]["spot"] = str(error)

    # Overall status: failed only if nothing was produced.
    produced = [
        result["options"], result["futures"], result["spot"],
    ]
    if not any(produced):
        result["status"] = "failed"
    elif result["errors"]:
        result["status"] = "partial"
    else:
        result["status"] = "downloaded"

    logger.info(
        "Date %s status: %s (errors: %s)",
        trading_date, result["status"],
        ", ".join(result["errors"].keys()) or "none",
    )

    return result


# ============================================================
# COMMAND LINE
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Download 1-min candles per trading date for: options (all "
            "active expiries across NIFTY, BANKNIFTY, SENSEX, split into "
            "NSE/BSE files), index futures (near/next/far, all indexes), "
            "and index spot. Saves per-day files under data/date=YYYY-MM-DD/."
        )
    )

    parser.add_argument(
        "--date",
        help="Single date (YYYY-MM-DD). Sets both start and end.",
    )
    parser.add_argument(
        "--start-date", default=START_DATE,
        help="First date (YYYY-MM-DD). Default: today IST.",
    )
    parser.add_argument(
        "--end-date", default=END_DATE,
        help="Last date (YYYY-MM-DD). Default: today IST.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DATA_DIR,
    )
    parser.add_argument(
        "--token-file", type=Path, default=TOKEN_FILE,
    )
    parser.add_argument(
        "--underlying", action="append",
        choices=sorted(UNDERLYINGS.keys()),
        help="Underlying to collect. Repeat for multiple. Default: all.",
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main():
    args = parse_args()

    global START_DATE, END_DATE, DATA_DIR, TOKEN_FILE

    if args.date:
        args.start_date = args.date
        args.end_date = args.date

    START_DATE = args.start_date
    END_DATE = args.end_date
    DATA_DIR = args.data_dir
    TOKEN_FILE = args.token_file

    selected_underlyings = (
        args.underlying if args.underlying else DEFAULT_UNDERLYINGS
    )

    start_time = time.time()

    logger.info("=" * 80)
    logger.info("FYERS COLLECTOR - OPTIONS + FUTURES + SPOT")
    logger.info("=" * 80)
    logger.info("Underlyings   : %s", ", ".join(selected_underlyings))
    logger.info("Date range    : %s -> %s", START_DATE, END_DATE)
    logger.info("Strike count  : %s (per side of ATM)", STRIKE_COUNT)
    logger.info("Futures       : %s", ", ".join(config.FUTURES_UNDERLYINGS))
    logger.info("Spot          : %s", ", ".join(config.SPOT_UNDERLYINGS.keys()))

    # Parse dates
    start_date = datetime.strptime(START_DATE, "%Y-%m-%d").date()
    end_date = datetime.strptime(END_DATE, "%Y-%m-%d").date()

    if end_date < start_date:
        raise ValueError("END_DATE cannot be before START_DATE.")

    # FYERS client
    logger.info("Creating FYERS client...")
    fyers = create_fyers_client(TOKEN_FILE)

    try:
        profile = fyers.get_profile()
        if profile.get("s") == "ok":
            name = profile.get("data", {}).get("name", "Unknown")
            fy_id = profile.get("data", {}).get("fy_id", "")
            logger.info("Logged in as: %s (%s)", name, fy_id)
        else:
            logger.warning(
                "Could not fetch profile: %s",
                profile.get("message", profile),
            )
    except Exception as e:
        logger.warning("Profile fetch failed: %s", e)

    logger.info("FYERS client ready.")

    # Date loop
    results = []
    total_weekends = 0
    total_holidays = 0

    current_date = start_date
    while current_date <= end_date:

        if current_date.weekday() >= 5:
            total_weekends += 1
            logger.info("Skipping weekend: %s", current_date)
            current_date += timedelta(days=1)
            continue

        if is_nse_holiday(current_date):
            total_holidays += 1
            logger.info(
                "Skipping NSE holiday: %s - %s",
                current_date, get_holiday_name(current_date),
            )
            current_date += timedelta(days=1)
            continue

        try:
            result = process_date(fyers, current_date, selected_underlyings)
        except Exception as error:  # noqa: BLE001 - safety net; process_date isolates internally
            logger.exception("FAILED: %s", current_date)
            result = {
                "date": current_date,
                "status": "failed",
                "options": None,
                "futures": None,
                "spot": None,
                "errors": {"fatal": str(error)},
            }
        results.append(result)

        current_date += timedelta(days=1)

    # ========================================================
    # SUMMARY
    # ========================================================

    runtime = time.time() - start_time

    dates_downloaded = sum(1 for r in results if r.get("status") == "downloaded")
    dates_partial = sum(1 for r in results if r.get("status") == "partial")
    dates_failed = sum(1 for r in results if r.get("status") == "failed")

    def _sum(kind, key):
        total = 0
        for r in results:
            part = r.get(kind)
            if isinstance(part, dict):
                total += part.get(key, 0) or 0
        return total

    # Options aggregates
    opt_contracts = _sum("options", "contracts")
    opt_with_data = _sum("options", "successful")
    opt_no_trades = _sum("options", "no_data")
    opt_failed = _sum("options", "failed")
    opt_rows = _sum("options", "rows")

    # Futures aggregates
    fut_contracts = _sum("futures", "contracts")
    fut_with_data = _sum("futures", "successful")
    fut_no_trades = _sum("futures", "no_data")
    fut_failed = _sum("futures", "failed")
    fut_rows = _sum("futures", "rows")

    # Spot aggregates
    spot_with_data = _sum("spot", "successful")
    spot_failed = _sum("spot", "failed")
    spot_rows = _sum("spot", "rows")

    logger.info("=" * 80)
    logger.info("COLLECTION SUMMARY")
    logger.info("=" * 80)
    logger.info("Underlyings            : %s", ", ".join(selected_underlyings))
    logger.info("Weekends skipped       : %s", total_weekends)
    logger.info("Holidays skipped       : %s", total_holidays)
    logger.info("Dates downloaded       : %s", dates_downloaded)
    logger.info("Dates partial          : %s", dates_partial)
    logger.info("Dates failed           : %s", dates_failed)
    logger.info("-" * 40)
    logger.info("OPTIONS  found=%s data=%s no_trades=%s failed=%s rows=%s",
                opt_contracts, opt_with_data, opt_no_trades, opt_failed, opt_rows)
    logger.info("FUTURES  found=%s data=%s no_trades=%s failed=%s rows=%s",
                fut_contracts, fut_with_data, fut_no_trades, fut_failed, fut_rows)
    logger.info("SPOT     indexes_with_data=%s failed=%s rows=%s",
                spot_with_data, spot_failed, spot_rows)
    logger.info("-" * 40)
    logger.info("Runtime                : %.1f seconds", runtime)
    logger.info("Output directory       : %s", DATA_DIR)
    logger.info("Log file               : %s", LOG_FILE)

    if dates_failed == 0 and dates_partial == 0:
        logger.info("SUCCESS: ALL DATES COLLECTED")
    elif dates_failed == 0:
        logger.warning(
            "PARTIAL: some data types were skipped on some dates - review log"
        )
    else:
        logger.error("RESULT: SOME DATES FAILED - INVESTIGATE")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
