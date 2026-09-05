"""One-time OAuth setup for Google Drive (personal Gmail accounts).

Why this exists
---------------
Google no longer gives service accounts any storage quota, so they cannot
CREATE files in a personal "My Drive" folder — even one shared with them.
Uploads fail with:

    403 storageQuotaExceeded: "Service Accounts do not have storage quota"

The fix is to let the pipeline act as *you* via OAuth. You authorise once
in a browser; Google returns a long-lived refresh token; the pipeline uses
it to mint access tokens forever after (no browser needed again, works in
GitHub Actions).

Steps
-----
1. Google Cloud Console -> APIs & Services -> Credentials
   -> Create credentials -> OAuth client ID -> Application type: "Desktop app"
   -> Download the JSON (call it client_secret.json). Do NOT commit it.
   (If the consent screen is in "Testing" mode, add your Gmail as a test
   user. Testing-mode refresh tokens expire after 7 days — publish the app,
   which for a personal project needs no verification, to make them
   permanent.)

2. Run:
       python drive_oauth_setup.py --client-secret client_secret.json

   A browser opens; sign in with the Google account that owns the Drive
   folders and approve access. The script writes drive_oauth_token.json.

3. Point the pipeline at the token:
     - locally:   GOOGLE_OAUTH_TOKEN_JSON=./drive_oauth_token.json   (in .env)
     - GitHub:    secret GOOGLE_OAUTH_TOKEN_JSON = the file's full contents

   drive_sync.py will then create/update files as you, using your quota.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

import drive_config


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--client-secret", default="client_secret.json", type=Path,
        help="OAuth Desktop-app client JSON downloaded from Google Cloud.",
    )
    parser.add_argument(
        "--output", default="drive_oauth_token.json", type=Path,
        help="Where to write the authorized-user token JSON.",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Print a URL to open manually instead of launching a browser "
             "(for headless machines).",
    )
    args = parser.parse_args()

    if not args.client_secret.exists():
        raise SystemExit(
            f"{args.client_secret} not found. Download an OAuth 'Desktop app' "
            "client JSON from Google Cloud Console -> Credentials."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(args.client_secret), scopes=drive_config.DRIVE_SCOPES
    )
    # access_type=offline + prompt=consent guarantees a refresh_token is
    # returned even if you have authorised this app before.
    if args.no_browser:
        credentials = flow.run_local_server(
            port=0, open_browser=False,
            authorization_prompt_message="Open this URL in a browser:\n{url}\n",
            access_type="offline", prompt="consent",
        )
    else:
        credentials = flow.run_local_server(
            port=0, access_type="offline", prompt="consent"
        )

    if not credentials.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Remove this app under "
            "https://myaccount.google.com/permissions and run again."
        )

    args.output.write_text(credentials.to_json(), encoding="utf-8")

    print(f"\nSaved OAuth token to {args.output}")
    print("Never commit this file.\n")
    print("Local:  add to .env ->  "
          f"{drive_config.OAUTH_TOKEN_ENV}=./{args.output.name}")
    print(f"GitHub: create secret {drive_config.OAUTH_TOKEN_ENV} with the "
          "file's full contents:\n")
    print(json.dumps(json.loads(args.output.read_text()), indent=2)
          .replace(credentials.refresh_token, "<refresh_token>")
          .replace(credentials.token or "", "<access_token>"))


if __name__ == "__main__":
    main()
