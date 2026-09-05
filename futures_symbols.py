"""Resolve active index futures contracts from the FYERS symbol master.

FYERS publishes a symbol master per segment (NSE_FO, BSE_FO) listing every
tradeable derivative. For futures collection we need, per index, the three
nearest monthly contracts (near / next / far) and a stable ticker that
matches the historical archive's continuous convention:

    NIFTY-I.NFO    near month
    NIFTY-II.NFO   next month
    NIFTY-III.NFO  far month

The master is large (>10 MB), so it is streamed to disk and parsed row by
row rather than held in memory. Column layouts differ slightly between
segments and have changed over time, so parsing is deliberately tolerant:
it locates the fields it needs by content (a dated FUT symbol, an epoch
expiry) rather than by fixed column index.

SENSEX resolution failures are surfaced to the caller as an empty result
plus a logged warning; the collector treats that as "skip SENSEX", never
as a fatal error.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path

import requests
import urllib3

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


IST = config.IST

# A fully-specified FYERS futures symbol looks like:
#   NSE:NIFTY26JANFUT      NSE:BANKNIFTY26FEBFUT      BSE:SENSEX26MARFUT
# i.e. <EXCH>:<ROOT><YY><MON>FUT   (monthly index futures have no strike).
_FUT_SYMBOL_RE = re.compile(
    r"^(?P<exch>[A-Z]+):(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mon>[A-Z]{3})FUT$"
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Where downloaded symbol masters are cached between calls within a run.
_CACHE_DIR = Path("cache")


def _cache_path(segment: str) -> Path:
    return _CACHE_DIR / f"symbol_master_{segment}.csv"


def download_symbol_master(segment: str, force: bool = False) -> Path:
    """Stream a segment's symbol master to disk and return its path.

    Cached per run: an existing file is reused unless force=True. The file
    is written to a temp path first and renamed, so an interrupted download
    cannot leave a half-written master that later parses to garbage.
    """
    url = config.SYMBOL_MASTER_URLS.get(segment)
    if not url:
        raise ValueError(f"No symbol-master URL configured for segment {segment}")

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = _cache_path(segment)

    if target.exists() and not force:
        logger.info("Using cached symbol master: %s", target)
        return target

    temp = target.with_suffix(".tmp")
    logger.info("Downloading symbol master %s -> %s", segment, target)

    with requests.get(url, stream=True, timeout=60, verify=False) as response:
        response.raise_for_status()
        with temp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    handle.write(chunk)

    temp.replace(target)
    return target


def _parse_expiry_epoch(value: str) -> date | None:
    """Interpret a symbol-master expiry field as a calendar date.

    The master stores expiry as an epoch (seconds). Some historical
    layouts used milliseconds; both are handled. Returns None if the
    value is not a usable timestamp.
    """
    value = (value or "").strip()
    if not value or not value.isdigit():
        return None
    try:
        epoch = int(value)
    except ValueError:
        return None

    # Milliseconds -> seconds, but only for values in the plausible ms
    # range (~1e12-1e13). Larger values are internal IDs, not timestamps,
    # and must be rejected rather than blindly divided.
    if 1_000_000_000_000 <= epoch < 10_000_000_000_000:
        epoch //= 1000

    # Sane seconds range: roughly 2001-06-19 .. 2035-01-01. Anything
    # outside this is not a real expiry epoch.
    if not (1_000_000_000 <= epoch <= 2_051_222_400):
        return None

    try:
        return datetime.fromtimestamp(epoch, tz=IST).date()
    except (OverflowError, OSError, ValueError):
        return None


def _expiry_from_symbol(match: re.Match) -> date | None:
    """Derive a month-level expiry date from a FUT symbol's YY+MON.

    Used as a fallback for ordering when the row carries no usable epoch.
    Day is set to 1; only year/month ordering matters for near/next/far.
    """
    month = _MONTHS.get(match.group("mon"))
    if not month:
        return None
    year = 2000 + int(match.group("yy"))
    try:
        return date(year, month, 1)
    except ValueError:
        return None


def _iter_fut_rows(master_path: Path, root: str, exchange: str):
    """Yield (symbol, expiry_date) for monthly FUT contracts of one index.

    Scans every cell of every row for a matching FUT symbol, then pairs it
    with the best expiry it can find (epoch field preferred, symbol month
    as fallback). Tolerant of unknown/rearranged columns.
    """
    root = root.upper()
    exchange = exchange.upper()

    with master_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row:
                continue

            symbol = None
            match = None
            symbol_index = -1
            for index, cell in enumerate(row):
                cell = (cell or "").strip()
                candidate = _FUT_SYMBOL_RE.match(cell)
                if candidate:
                    symbol = cell
                    match = candidate
                    symbol_index = index
                    break

            if not symbol or match is None:
                continue
            if match.group("root") != root:
                continue
            if match.group("exch") != exchange:
                continue

            # Expiry epoch. In both the NSE_FO and BSE_FO layouts the
            # expiry epoch sits in the cell immediately before the tradeable
            # symbol, e.g. ...,2026-09-03,1790244600,BSE:SENSEX26SEPFUT,...
            # Scanning "any digit cell" is wrong because the leading token
            # is a huge internal id that parses to a bogus far-future date;
            # anchor on the symbol's position instead.
            expiry = None
            if symbol_index > 0:
                expiry = _parse_expiry_epoch(row[symbol_index - 1])

            # Fallback: any cell that parses to a plausible expiry epoch.
            if expiry is None:
                for cell in row:
                    expiry = _parse_expiry_epoch(cell)
                    if expiry is not None:
                        break

            # Last resort: month implied by the symbol (YY+MON).
            if expiry is None:
                expiry = _expiry_from_symbol(match)
            if expiry is None:
                continue

            yield symbol, expiry


def resolve_index_futures(instrument: str, trading_date: date, master_path: Path | None = None):
    """Return the near/next/far futures contracts for one index.

    Result is a list of dicts in near -> far order:
        {
            "symbol":         "NSE:NIFTY26JANFUT",   # what history() needs
            "ticker":         "NIFTY-I.NFO",          # archive continuous form
            "contract_series":"I",
            "expiry":          date(2026, 1, 29),
            "underlying":     "NIFTY",
        }

    Contracts already expired relative to trading_date are dropped, then the
    nearest FUTURES_CONTRACT_COUNT by expiry are kept.
    """
    spec = config.FUTURES_SYMBOLS.get(instrument)
    if not spec:
        raise ValueError(f"No futures symbol config for {instrument}")

    segment = spec["segment"]
    root = spec["root"]
    exchange = spec["exchange"]

    if master_path is None:
        master_path = download_symbol_master(segment)

    # Deduplicate on symbol; a symbol can appear on multiple rows.
    by_symbol: dict[str, date] = {}
    for symbol, expiry in _iter_fut_rows(master_path, root, exchange):
        if expiry < trading_date:
            continue
        # Keep the earliest expiry seen for a symbol (they should agree).
        existing = by_symbol.get(symbol)
        if existing is None or expiry < existing:
            by_symbol[symbol] = expiry

    if not by_symbol:
        raise RuntimeError(
            f"No active {instrument} futures found in {master_path} "
            f"on or after {trading_date}."
        )

    ordered = sorted(by_symbol.items(), key=lambda item: (item[1], item[0]))
    nearest = ordered[: config.FUTURES_CONTRACT_COUNT]

    contracts = []
    for position, (symbol, expiry) in enumerate(nearest):
        series = config.FUTURES_CONTRACT_SERIES[position]
        contracts.append({
            "symbol": symbol,
            "ticker": f"{root}-{series}.NFO",
            "contract_series": series,
            "expiry": expiry,
            "underlying": instrument,
        })

    logger.info(
        "%s futures resolved: %s",
        instrument,
        ", ".join(f"{c['contract_series']}={c['symbol']}({c['expiry']})" for c in contracts),
    )
    return contracts


def resolve_all_futures(trading_date: date):
    """Resolve futures for every configured index.

    Returns (contracts, skipped) where contracts is a flat list across all
    indexes and skipped maps instrument -> reason for indexes that could not
    be resolved. A failure for any single index (e.g. SENSEX/BSE access) is
    logged and skipped rather than aborting the whole run.
    """
    all_contracts = []
    skipped: dict[str, str] = {}

    for instrument in config.FUTURES_UNDERLYINGS:
        try:
            contracts = resolve_index_futures(instrument, trading_date)
            all_contracts.extend(contracts)
        except Exception as error:  # noqa: BLE001 - isolate per-index failure
            logger.warning(
                "Skipping %s futures: could not resolve contracts (%s)",
                instrument, error,
            )
            skipped[instrument] = str(error)

    return all_contracts, skipped
