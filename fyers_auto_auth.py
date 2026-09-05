from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class FyersAutoAuth:
    """Automated FYERS login using credentials + TOTP secret.

    No manual OTP/TOTP input is requested. The current TOTP is generated
    from TOTP_SECRET. If FYERS changes the authentication contract, this
    module will fail explicitly rather than bypassing security controls.
    """

    BASE_VAGATOR = "https://api-t2.fyers.in/vagator/v2"
    BASE_API = "https://api-t1.fyers.in/api/v3"

    def __init__(
        self,
        fy_id: str,
        app_id_type: str,
        pin: str,
        app_id: str,
        app_type: str,
        app_secret: str,
        redirect_uri: str,
        totp_secret: str,
        token_file: str = "./fyers_access_token.json",
        timeout: int = 30,
    ):
        self.fy_id = fy_id.strip()
        self.app_id_type = str(app_id_type).strip()
        self.pin = pin.strip()
        self.app_id = app_id.strip()
        self.app_type = str(app_type).strip()
        self.app_secret = app_secret.strip()
        self.redirect_uri = redirect_uri.strip()
        self.totp_secret = totp_secret.strip()
        self.token_file = Path(token_file)
        self.timeout = timeout

        if not all([
            self.fy_id, self.pin, self.app_id, self.app_secret,
            self.redirect_uri, self.totp_secret
        ]):
            raise ValueError("One or more FYERS credentials are missing.")

    @property
    def app_id_hash(self) -> str:
        value = f"{self.app_id}-{self.app_type}:{self.app_secret}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        response = requests.post(
            url,
            json=payload,
            headers=headers or {},
            timeout=self.timeout,
            verify=False,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw_response": response.text}

        if not response.ok:
            raise RuntimeError(
                f"FYERS request failed ({response.status_code}) "
                f"for {url}: {data}"
            )
        return data

    def _generate_totp(self) -> str:
        return pyotp.TOTP(self.totp_secret).now()

    def _send_login_otp(self) -> str:
        data = self._post(
            f"{self.BASE_VAGATOR}/send_login_otp",
            {"fy_id": self.fy_id, "app_id": self.app_id_type},
        )
        if "request_key" not in data:
            raise RuntimeError(f"FYERS send_login_otp response: {data}")
        return data["request_key"]

    def _verify_totp(self, request_key: str) -> str:
        data = self._post(
            f"{self.BASE_VAGATOR}/verify_otp",
            {
                "request_key": request_key,
                "otp": self._generate_totp(),
            },
        )
        if "request_key" not in data:
            raise RuntimeError(f"FYERS verify_otp response: {data}")
        return data["request_key"]

    def _verify_pin(self, request_key: str) -> str:
        data = self._post(
            f"{self.BASE_VAGATOR}/verify_pin",
            {
                "request_key": request_key,
                "identity_type": "pin",
                "identifier": self.pin,
            },
        )
        try:
            return data["data"]["access_token"]
        except (KeyError, TypeError):
            raise RuntimeError(f"FYERS verify_pin response: {data}")

    def _get_auth_code(self, login_access_token: str) -> str:
        payload = {
            "fyers_id": self.fy_id,
            "app_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "appType": self.app_type,
            "code_challenge": "",
            "state": "fyers_auto_login",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        }

        response = requests.post(
            f"{self.BASE_API}/token",
            json=payload,
            headers={"Authorization": f"Bearer {login_access_token}"},
            timeout=self.timeout,
            allow_redirects=False,
            verify=False,
        )

        if response.status_code not in (302, 303, 307, 308):
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(
                f"FYERS token endpoint failed ({response.status_code}): {detail}"
            )

        location = response.headers.get("Location")
        if not location:
            try:
                location = response.json().get("Url")
            except ValueError:
                location = None

        if not location:
            raise RuntimeError("FYERS token endpoint did not return a redirect URL.")

        query = parse_qs(urlparse(location).query)
        auth_code = query.get("auth_code", [None])[0]

        if not auth_code:
            raise RuntimeError(
                "FYERS redirect did not contain auth_code. "
                f"Redirect returned: {location}"
            )
        return auth_code

    def _exchange_auth_code(self, auth_code: str) -> str:
        data = self._post(
            f"{self.BASE_API}/validate-authcode",
            {
                "grant_type": "authorization_code",
                "appIdHash": self.app_id_hash,
                "code": auth_code,
            },
        )
        if "access_token" not in data:
            raise RuntimeError(f"FYERS validate-authcode response: {data}")
        return data["access_token"]

    def login(self) -> str:
        request_key = self._send_login_otp()
        request_key = self._verify_totp(request_key)
        login_access_token = self._verify_pin(request_key)
        auth_code = self._get_auth_code(login_access_token)
        access_token = self._exchange_auth_code(auth_code)

        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(
            json.dumps(
                {"access_token": access_token},
                indent=2,
            ),
            encoding="utf-8",
        )
        return access_token

    def load_saved_token(self) -> str | None:
        if not self.token_file.exists():
            return None
        try:
            return json.loads(self.token_file.read_text(encoding="utf-8"))["access_token"]
        except (KeyError, json.JSONDecodeError):
            return None
