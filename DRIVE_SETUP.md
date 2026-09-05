> ## ⚠️ Read first: personal Gmail needs OAuth, not a service account
>
> Google **no longer gives service accounts storage quota**. A service account
> can still *update* a file you already own, but any attempt to *create* a
> file (a new per-year parquet, a `.dates.json` sidecar, an archive copy) in a
> personal My Drive fails with:
>
> `403 storageQuotaExceeded — Service Accounts do not have storage quota`
>
> **Fix (one-time, ~5 min):**
> 1. Google Cloud Console → APIs & Services → Credentials → **Create OAuth client ID** → type **Desktop app** → download JSON as `client_secret.json`.
>    If the OAuth consent screen is in *Testing*, add your Gmail as a test user **and then click "Publish app"** (no verification needed for personal use) — otherwise the token expires every 7 days.
> 2. `pip install -r requirements.txt` then `python drive_oauth_setup.py` — sign in as the account that owns the Drive folders.
> 3. It writes `drive_oauth_token.json`. Locally put `GOOGLE_OAUTH_TOKEN_JSON=./drive_oauth_token.json` in `.env`; on GitHub add secret **`GOOGLE_OAUTH_TOKEN_JSON`** with the file's full contents.
>
> When that variable is set it takes precedence over `GOOGLE_SERVICE_ACCOUNT_JSON`.
> Folder IDs in `drive_config.py` stay the same. The service-account path (Parts A–C below)
> only works if you have a Google Workspace **shared drive**.

# Setup: Daily collection to Google Drive + Telegram (free)

This guide sets up the fully automated daily pipeline:

```
login.py  →  production_collector.py  →  drive_sync.py  →  send_to_telegram.py
```

GitHub Actions runs it on a schedule. The script authenticates to FYERS,
collects options + futures + spot, appends each into the per-year "source
of truth" files on **your** Google Drive, and sends the day's files to
Telegram. Everything here uses free tiers (Google Cloud service account,
Drive API, GitHub Actions).

There is no server and no manual login. The only one-time work is creating
a Google service account and pasting a few secrets into GitHub.

---

## How it works (the part that confuses people)

GitHub Actions runs your script on a temporary Linux machine. That machine
talks to Google Drive over the internet using the **Drive API**. To be
allowed in, it uses a **service account** — a robot Google account with its
own email and a JSON key.

You share your Drive folders with the robot's email (just like sharing with
a person). The script reads the JSON key from a GitHub secret, authenticates,
and can then download/upload files in those shared folders. No browser, no
OTP, fully unattended.

Your files stay owned by **you**. The script edits the existing per-year
files in place (same file), so ownership never moves to the robot.

---

## Part A — Google Cloud: create the service account (free)

1. Go to <https://console.cloud.google.com/> and sign in with your Gmail.
2. Create a project (top bar → project dropdown → **New Project**). Name it
   anything, e.g. `fyers-daily`. Free.
3. Enable the Drive API: search **"Google Drive API"** in the top search
   bar → open it → **Enable**.
4. Create the service account:
   - Left menu → **APIs & Services → Credentials**.
   - **Create Credentials → Service account**.
   - Give it a name (e.g. `fyers-drive`) → **Create and continue** → skip
     the optional roles → **Done**.
5. Create its key:
   - Click the new service account → **Keys** tab → **Add key → Create new
     key → JSON → Create**.
   - A `.json` file downloads. **This is the secret you paste into GitHub.**
     Keep it safe; do not commit it.
6. Copy the service account's **email** (looks like
   `fyers-drive@fyers-daily.iam.gserviceaccount.com`). You need it in Part B.

---

## Part B — Share your Drive folders with the robot

In Google Drive you should already have (or create) these folders:

- **Futures Data** — holds `INDEX_FUTURES_<year>.parquet`
- **Options Data** — holds `NSE_INDEX_OPTIONS_<year>.parquet` and
  `BSE_INDEX_OPTIONS_<year>.parquet`
- **Spot Data** — holds `Nifty 50.csv`, `Nifty Bank.csv`, `Sensex.csv`
- **(optional) Archive** — raw per-day files are copied here for safety

For each folder:

1. Right-click → **Share**.
2. Paste the service account **email** from Part A.
3. Set its role to **Editor**.
4. Send/Save.

> Personal Gmail note: a service account cannot *own* files in a personal
> Drive, but with **Editor** access to these folders it can read, append to,
> and update the files inside them — which is all this pipeline needs.

### Get each folder's ID

Open a folder in Drive and look at the URL:

```
https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                        └────────── folder ID ──────────┘
```

Copy the ID (the part after `/folders/`).

### Put the IDs in the code

Edit `drive_config.py` and fill in the four IDs:

```python
DRIVE_FOLDERS = {
    "options": "PASTE_OPTIONS_DATA_FOLDER_ID",
    "futures": "PASTE_FUTURES_DATA_FOLDER_ID",
    "spot":    "PASTE_SPOT_DATA_FOLDER_ID",
}
DRIVE_ARCHIVE_FOLDER = "PASTE_ARCHIVE_FOLDER_ID"   # or set to None to disable
```

Commit and push that change. (Folder IDs are not secret; the JSON key is.)

---

## Part C — GitHub secrets

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add each of these:

| Secret name                   | What it is                                             |
| ----------------------------- | ------------------------------------------------------ |
| `GOOGLE_OAUTH_TOKEN_JSON`     | **Personal Gmail:** contents of `drive_oauth_token.json` (see top of this doc) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Workspace shared drive only:** the JSON key from Part A |
| `FY_ID`                       | FYERS client/user id                                   |
| `APP_ID_TYPE`                 | FYERS app id type                                      |
| `PIN`                         | FYERS PIN                                              |
| `APP_ID`                      | FYERS app id                                           |
| `APP_TYPE`                    | FYERS app type (e.g. `100`)                            |
| `APP_SECRET`                  | FYERS app secret                                       |
| `REDIRECT_URI`                | FYERS redirect URI                                     |
| `TOTP_SECRET`                 | FYERS TOTP secret (for unattended 2FA)                 |
| `TELEGRAM_BOT_TOKEN`          | Telegram bot token                                     |
| `TELEGRAM_CHAT_ID`            | Telegram chat id to send files to                      |

For `GOOGLE_SERVICE_ACCOUNT_JSON`, open the downloaded `.json` file, copy
**everything** (including the outer `{ }`), and paste it as the secret value.

---

## Part D — Run it

The workflow (`.github/workflows/daily.yml`) is already set to run
**Mon-Fri at 16:30 IST** (one hour after market close).

To test immediately without waiting for the schedule:

1. GitHub repo → **Actions** tab → **Daily FYERS Collection** → **Run
   workflow**.
2. Optionally type a date (`YYYY-MM-DD`); leave blank for today.
3. Watch the run. On success you get the files in Telegram and appended in
   Drive.

### Run locally instead (optional)

Put the same values in a local `.env` file and set
`GOOGLE_SERVICE_ACCOUNT_JSON` to the **path** of your key file, then:

```
python login.py
python production_collector.py
python drive_sync.py
python send_to_telegram.py
```

Each of the last three accepts `--date YYYY-MM-DD` for backfilling a
specific day.

---

## Safety and behavior notes

- **Idempotent**: each per-year file has a hidden `.<name>.dates.json`
  sidecar listing the dates already ingested. Re-running a day is skipped,
  and duplicate rows are dropped on natural keys — so retries never corrupt
  or double-count data.
- **Never destructive**: the old per-year file is only overwritten after the
  new merged file is written locally and verified. A failed run leaves the
  last-good file intact on Drive.
- **Archive**: if you set `DRIVE_ARCHIVE_FOLDER`, the raw per-day files are
  also copied there, so a year can be rebuilt if needed.
- **Timing**: the daily append is fast; the FYERS options collection is the
  slow part (many API calls). The whole run typically finishes well inside
  the 60-minute workflow timeout.
- **First run of a year**: if `INDEX_FUTURES_2026.parquet` (etc.) does not
  exist yet, it is created automatically. You can backfill earlier months
  later.

---

## Troubleshooting

- **`Drive folder IDs are not configured`** — you did not fill in
  `DRIVE_FOLDERS` in `drive_config.py`.
- **`GOOGLE_SERVICE_ACCOUNT_JSON is not set`** — the GitHub secret is missing
  or misnamed.
- **`Ambiguous: N files named ...`** — you have duplicate files with the same
  name in a Drive folder; delete/rename the extras.
- **Drive 403 / permission denied** — the folder was not shared with the
  service account email, or shared as Viewer instead of Editor.
- **FYERS auth fails** — a FYERS secret is wrong, or the token expired; the
  workflow logs in fresh each run, so re-check the FYERS secrets.
