# FYERS Daily Market Data Pipeline

Collects **1-minute market data** from the FYERS API every trading day and
keeps a growing per-year history on Google Drive, with a copy of each day's
files sent to Telegram. Three data types are collected:

- **Options** — OHLC + Volume + Open Interest for **NIFTY**, **BANKNIFTY**,
  **SENSEX**, across every expiry actively trading that day. Saved split by
  exchange (NSE and BSE).
- **Futures** — the three nearest monthly index-futures contracts
  (near / next / far) for all three indexes.
- **Spot** — the underlying index level for all three indexes.

The whole thing runs unattended from GitHub Actions: authenticate → collect →
append to Google Drive → send to Telegram.

---

## Pipeline at a glance

```
login.py                 get a fresh daily FYERS token
   │
production_collector.py  collect options + futures + spot -> data/date=YYYY-MM-DD/
   │
drive_sync.py            append each file into the per-year "source of truth" on Drive
   │
send_to_telegram.py      send the day's files to Telegram, each captioned
```

On GitHub Actions, `.github/workflows/daily.yml` runs all four in order,
Mon-Fri after the market close.

---

## Daily local outputs

One directory per trading date under `data/`:

```
data/date=YYYY-MM-DD/
    options_nse.parquet     NIFTY + BANKNIFTY options
    options_bse.parquet     SENSEX options
    futures.parquet         all three indexes, near/next/far
    spot_Nifty 50.csv       NIFTY spot
    spot_Nifty Bank.csv     BANKNIFTY spot
    spot_Sensex.csv         SENSEX spot
```

These are the raw daily files. `drive_sync.py` appends them into the per-year
files on Drive.

---

## Google Drive layout (the history)

`drive_sync.py` maintains one **per-year "source of truth" file** per data
type, matching the existing historical archive:

| Drive folder   | File(s)                                                        | Fed by |
|----------------|----------------------------------------------------------------|--------|
| `Options Data` | `NSE_INDEX_OPTIONS_<year>.parquet`, `BSE_INDEX_OPTIONS_<year>.parquet` | `options_nse.parquet`, `options_bse.parquet` |
| `Futures Data` | `INDEX_FUTURES_<year>.parquet`                                 | `futures.parquet` |
| `Spot Data`    | `Nifty 50.csv`, `Nifty Bank.csv`, `Sensex.csv` (cumulative)    | the three spot CSVs |
| `Archive`      | dated copies of every raw daily file (optional)                | all of the above |

Each per-year file has a hidden `.<name>.dates.json` sidecar listing the dates
already ingested. This makes appends **idempotent**: re-running a day is
skipped, and duplicate rows are dropped on natural keys even if the sidecar is
lost.

Full Drive/GitHub setup is in **[DRIVE_SETUP.md](DRIVE_SETUP.md)**.

---

## Quick start (local)

```powershell
# 1. Install
pip install -r requirements.txt

# 2. Configure credentials (see the table below), in a local .env
#    Never commit .env or fyers_access_token.json.

# 3. Authenticate (daily — FYERS tokens last one trading day)
python login.py

# 4. Collect today's options + futures + spot
python production_collector.py

# 5. Append to Google Drive (needs drive_config.py + service account set up)
python drive_sync.py

# 6. Send the day's files to Telegram
python send_to_telegram.py
```

Steps 4-6 each accept `--date YYYY-MM-DD` for a specific day.

---

## Credentials

Set these in a local `.env` (for local runs) and as **GitHub secrets** (for the
scheduled workflow). See [DRIVE_SETUP.md](DRIVE_SETUP.md) for the full secret
list and how to add them.

### FYERS (used by `login.py`)

| Variable | What it is |
|----------|-----------|
| `FY_ID` | Your FYERS client ID |
| `APP_ID_TYPE` | Login app type (normally `2`) |
| `PIN` | Your 4-digit FYERS PIN |
| `APP_ID` | API app ID |
| `APP_TYPE` | API app type (normally `100`) |
| `APP_SECRET` | API app secret |
| `REDIRECT_URI` | Must match the app config exactly |
| `TOTP_SECRET` | Base32 secret behind your 2FA QR code (the raw secret, not the 6-digit code) — this is what makes login unattended |
| `TOKEN_FILE` | Where to save the token (optional; defaults to `./fyers_access_token.json`) |

### Google Drive (used by `drive_sync.py`)

| Variable | What it is |
|----------|-----------|
| `GOOGLE_OAUTH_TOKEN_JSON` | **Use this for personal Gmail.** OAuth token from `python drive_oauth_setup.py`. Inline JSON in CI; a file path locally. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Alternative for Google Workspace shared drives only (service accounts cannot create files in a personal My Drive). |

### Telegram (used by `send_to_telegram.py`)

| Variable | What it is |
|----------|-----------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Chat ID to send files to |

---

## What each file does

### Run these

| File | What it does | When |
|------|--------------|------|
| `login.py` | Authenticates with FYERS via TOTP, saves the daily access token. | Every day, first |
| `production_collector.py` | Collects options + futures + spot for the date range and writes the per-day files. | Every day |
| `drive_sync.py` | Appends the per-day files into the per-year files on Google Drive (idempotent, atomic, archived). | Every day |
| `send_to_telegram.py` | Sends the day's files to Telegram, each captioned with date and contents. | Every day |

### Collectors (imported by `production_collector.py`)

| File | Role |
|------|------|
| `futures_collector.py` | Downloads 1-min futures candles for the resolved contracts. |
| `futures_symbols.py` | Resolves the near/next/far monthly futures per index from the FYERS symbol master, mapping them to the historical `NIFTY-I/-II/-III.NFO` continuous tickers. |
| `spot_collector.py` | Downloads 1-min index (spot) candles, one CSV per index. |
| `fyers_common.py` | Shared FYERS client, retry/no-data policy, IST candle conversion, and atomic parquet/CSV writers used by every collector. |

### Drive layer

| File | Role |
|------|------|
| `drive_config.py` | **Edit this once.** All Drive settings in one place: folder IDs, per-year file names, dedup keys, service-account env var. |
| `drive_client.py` | Thin Google Drive API wrapper (find / download / upload / atomic replace). Auth via OAuth token or service account. |
| `drive_oauth_setup.py` | **Run once.** Browser sign-in that produces the OAuth token for personal-Gmail Drive access. |

### Config & support

| File | Role |
|------|------|
| `config.py` | Index symbols, strike count, retry settings, paths, and all schemas (options 13-col, futures 12-col, spot 6-col), plus per-day output filenames. |
| `nse_holidays.py` | NSE F&O holiday calendar. **Update each year.** |
| `fyers_auto_auth.py` | Authentication implementation used by `login.py`. |
| `.github/workflows/daily.yml` | Scheduled GitHub Actions workflow running the full chain. |
| `DRIVE_SETUP.md` | Step-by-step free setup for the Drive service account and GitHub secrets. |

---

## Schemas

### Options (`options_nse.parquet`, `options_bse.parquet`)

`Ticker, Date, Time, Open, High, Low, Close, Volume, OpenInterest, Underlying,
Expiry, Strike, OptionType`

NSE file = NIFTY + BANKNIFTY. BSE file = SENSEX. (The append target on Drive is
the "enriched" per-year file; if it carries extra columns, the daily rows are
conformed to match on first append.)

### Futures (`futures.parquet`)

`Ticker, Date, Time, Open, High, Low, Close, Volume, OpenInterest, SourceFile,
Underlying, ContractSeries`

- `Ticker` uses the historical continuous convention: `NIFTY-I.NFO`,
  `NIFTY-II.NFO`, `NIFTY-III.NFO` (and BANKNIFTY, SENSEX).
- `ContractSeries` is `I` / `II` / `III` (near / next / far).
- `SourceFile` is `fyers_daily_extraction` for live-collected rows.

### Spot (`Nifty 50.csv`, `Nifty Bank.csv`, `Sensex.csv`)

`timestamp, Open, High, Low, Close, Volume`

`timestamp` is a full IST datetime so rows stay unique across days in the
cumulative history. No open interest (an index has none).

---

## `production_collector.py` options

```powershell
# Today (default), all three indexes, all active expiries
python production_collector.py

# One specific day
python production_collector.py --date 2026-08-26

# A date range
python production_collector.py --start-date 2026-08-24 --end-date 2026-08-26

# One or more specific indexes
python production_collector.py --underlying NIFTY --underlying BANKNIFTY

# Write elsewhere / use a specific token file
python production_collector.py --date 2026-08-26 --data-dir .\scratch
python production_collector.py --token-file .\fyers_access_token.json
```

Re-running a day is safe. Options are skipped if both split files already exist
and validate; futures/spot skip on their own existing files.

---

## Scheduling on GitHub Actions

`.github/workflows/daily.yml` runs Mon-Fri at **16:30 IST** (`0 11 * * 1-5`
UTC), one hour after the 15:30 IST close so FYERS has finalized the day's
candles. It also has a manual **Run workflow** button (Actions tab) with an
optional date input for backfills.

The workflow: install deps → `login.py` → `production_collector.py` →
`drive_sync.py` → `send_to_telegram.py`. Telegram and log upload run even if an
earlier step fails, so you still get whatever was collected. All credentials
come from repo secrets (see [DRIVE_SETUP.md](DRIVE_SETUP.md)).

To run it locally on a schedule instead, use Windows Task Scheduler with the
four commands from Quick start, and set "Start in" to the project directory so
`data/` and `logs/` resolve correctly.

---

## How collection works

```
python login.py
  .env / secrets
    -> send_login_otp -> verify_otp (TOTP) -> verify_pin
    -> token (OAuth auth_code) -> validate-authcode -> access token
  -> fyers_access_token.json

python production_collector.py
  load token, warn if not issued today
  for each date in range:
      skip weekends and NSE holidays (nse_holidays.py)
      OPTIONS  for NIFTY, BANKNIFTY, SENSEX:
          optionchain(timestamp="")     -> expiry list (keep within 5 weeks)
          optionchain(timestamp=epoch)  -> each expiry's contracts
          history()                     -> 1-min candles per contract
          validate -> save split by exchange (NSE / BSE)
      FUTURES  resolve near/next/far from symbol master, history() per contract
      SPOT     history() per index
      each data type is isolated: one failing type does not stop the others
```

**Safety properties**

- **Idempotent** — a date already collected/ingested is skipped.
- **Atomic writes** — data lands in a temp file, is read back and verified, then
  renamed (locally) or replaced by file id (on Drive). An interrupted run cannot
  leave a half-written or corrupt file; the last-good file on Drive is never
  destroyed until the new one is verified.
- **Never fabricates candles** — minutes FYERS does not return stay missing.
- **Retries only real faults** — a definitive "no trades" (`s='no_data'`) is not
  retried.
- **Failures are isolated** — one bad contract, index, or data type is logged and
  counted, not fatal. SENSEX failures never abort the NSE data.

---

## Reading the run summary

The collector prints a per-type summary:

```
OPTIONS  found=738 data=415 no_trades=318 failed=5 rows=...
FUTURES  found=9 data=9 no_trades=0 failed=0 rows=...
SPOT     indexes_with_data=3 failed=0 rows=...
```

| Field | Meaning | Action |
|-------|---------|--------|
| `data` | Contracts with candles saved | None |
| `no_trades` | FYERS returned `s='no_data'` — the contract exists but never traded | None — expected, often large for far-from-ATM/far-dated strikes |
| `failed` | A real error (network, auth, unexpected response) | Investigate |

Only `failed` matters. A large `no_trades` count is normal and tracks liquidity.

---

## Known FYERS API behaviour

- **One expiry per option-chain call.** `optionchain` returns a single expiry's
  contracts; the collector calls it once per expiry.
- **Expiry is selected by epoch, not date string.** The `timestamp` parameter is
  the epoch straight from `expiryData` (e.g. `1788257400`).
- **Past expiries can't be backfilled.** Once contracts expire they disappear
  from the chain (`code -99 Bad request`), so collect each day while its
  contracts are still listed. Hard API limitation.
- **Dead strikes return `s='no_data'`** with a blank message — treated as
  terminal so no retries/backoff are wasted.
- **Futures symbols come from the FYERS symbol master** (`public.fyers.in`,
  streamed to disk). The expiry epoch sits in the cell before the symbol; the
  resolver anchors on that and maps the three nearest months to I/II/III.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Token file not found` / `Token saved on <date>` warning | Not logged in, or stale token | `python login.py` |
| Auth errors on every contract | Expired token | `python login.py` |
| `CERTIFICATE_VERIFY_FAILED` | Corporate proxy intercepting TLS | Already handled: FYERS endpoints skip verification |
| `code -99 Bad request` on option chain | Collecting a past date whose contracts expired | Cannot be fixed; collect current dates |
| `Drive folder IDs are not configured` | `DRIVE_FOLDERS` still has placeholders | Fill in `drive_config.py` (see DRIVE_SETUP.md) |
| `GOOGLE_SERVICE_ACCOUNT_JSON is not set` | Missing/misnamed secret | Add the GitHub secret |
| Drive `403` / permission denied | Folder not shared with the service account, or shared as Viewer | Share as **Editor** with the SA email |
| `Ambiguous: N files named ...` | Duplicate files with the same name in a Drive folder | Remove/rename the extras |
| SENSEX missing from futures | (fixed) symbol-master expiry parsing | Ensure `futures_symbols.py` is current |
| Holiday not skipped | Missing from the calendar | Add it to `nse_holidays.py` |

---

## Maintenance

- **Yearly:** refresh `nse_holidays.py` from the official NSE circular. A new
  per-year Drive file is created automatically on the first run of a new year.
- **Daily:** confirm `login.py` succeeded before the collector runs (the
  workflow does this in order).
- **Occasionally:** review `logs/production_collector.log` for repeated `failed`
  counts (not `no_trades`), which can signal a FYERS symbol-format change.

## Operating rules

- Do not overwrite good data files or fabricate missing candles.
- Fix authentication by running `login.py`; don't work around an expired token
  in collector logic.
- Keep all NSE F&O holidays in `nse_holidays.py`.
- Never commit `.env`, `fyers_access_token.json`, or the service-account key.
  (`.gitignore` covers these.)
