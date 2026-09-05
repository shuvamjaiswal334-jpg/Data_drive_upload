import os
from dotenv import load_dotenv
from fyers_auto_auth import FyersAutoAuth

load_dotenv()

required = [
    "FY_ID", "APP_ID_TYPE", "PIN", "APP_ID", "APP_TYPE",
    "APP_SECRET", "REDIRECT_URI", "TOTP_SECRET"
]
missing = [x for x in required if not os.getenv(x)]
if missing:
    raise SystemExit("Missing .env values: " + ", ".join(missing))

auth = FyersAutoAuth(
    fy_id=os.environ["FY_ID"],
    app_id_type=os.environ["APP_ID_TYPE"],
    pin=os.environ["PIN"],
    app_id=os.environ["APP_ID"],
    app_type=os.environ["APP_TYPE"],
    app_secret=os.environ["APP_SECRET"],
    redirect_uri=os.environ["REDIRECT_URI"],
    totp_secret=os.environ["TOTP_SECRET"],
    token_file=os.getenv("TOKEN_FILE", "./fyers_access_token.json"),
)

print("Starting automated FYERS authentication...")
print("No manual OTP/TOTP input will be requested.")
token = auth.login()
print("Authentication successful.")
print("Access token saved locally.")
print("Token value is intentionally not printed.")
