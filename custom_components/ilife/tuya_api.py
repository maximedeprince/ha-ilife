"""Low-level Tuya OpenAPI client (Cloud Project, HMAC-SHA256 signing) for ILIFE Clean.

ILIFE Clean-branded vacuums (e.g. the T20s) run on Tuya's white-label IoT platform, not
the proprietary Alibaba backend used by ILIFEHOME: during Wi-Fi setup the T20s creates a
"SmartLife-XXXX" pairing hotspot, Tuya's own stock AP-mode SSID, and the ILIFE Clean app
itself documents linking with the Smart Life app / Tuya Smart Life skill for Alexa.

Tuya restricts password-based Cloud API login (the "Smart Home" authorized-login grant)
to its own first-party apps (Tuya Smart / Smart Life) — an OEM app's account cannot be
logged into directly this way without Tuya's private per-app "schema" identifier, which
is not published anywhere and would have to be guessed. Instead this client uses the
standard, documented path for third-party/OEM Tuya accounts: a user-owned Tuya IoT Cloud
Project (free, created at https://iot.tuya.com) authenticates itself with its own
Access ID/Secret (no password involved), and the ILIFE Clean account is authorized into
that project once via the project's "Link Tuya App Account" QR code (scanned from inside
the ILIFE Clean app). That yields a UID used to address the linked account's devices.
See README for the one-time setup steps.

Synchronous (urllib): call from HA via hass.async_add_executor_job — mirrors api.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

_LOGGER = logging.getLogger(__name__)

# Tuya Cloud data-center endpoints (developer.tuya.com / tuya-home-assistant wiki).
# Accounts registered outside these are routed to the Western America DC by Tuya itself.
TUYA_REGIONS = {
    "eu": "openapi.tuyaeu.com",
    "us": "openapi.tuyaus.com",
    "cn": "openapi.tuyacn.com",
    "in": "openapi.tuyain.com",
}
DEFAULT_TUYA_REGION = "eu"

TOKEN_TTL_SAFETY = 60  # seconds; refresh this long before actual expiry


class TuyaError(Exception):
    """Generic Tuya API error, carrying the Tuya error code when the server sent one."""

    def __init__(self, message: str, code: int | str | None = None) -> None:
        super().__init__(message)
        self.code = code


class TuyaAuthError(TuyaError):
    """Bad Access ID/Secret, or the account UID isn't linked to this Cloud Project."""


class TuyaTokenExpiredError(TuyaAuthError):
    """Access token expired/invalid (code 1010) — caller should refresh and retry."""


class TuyaNoDevicesError(TuyaError):
    """Project credentials are valid but the UID owns no devices (wrong/unlinked UID)."""


class TuyaOfflineError(TuyaError):
    """Device reports offline — command not delivered."""


# Tuya OpenAPI codes that mean "the Cloud Project/UID setup is wrong", not "try again
# later" — these must surface as an authentication problem so the user fixes the setup
# instead of waiting for a retry that can never succeed.
_AUTH_CODES = {1004, 1005, 1010, 1106, 2406, 28841002, 28841101, 28841105}

# The setup mistakes users actually hit (see issues #3 and #13): a bare "Tuya API error
# (1106): permission deny" tells nobody anything, so each known code gets the fix.
_CODE_HINTS: dict[int, str] = {
    1004: "signature rejected — check the Access Secret was pasted in full (no stray "
          "spaces), and that the Home Assistant host clock is correct",
    1005: "unknown Access ID — check the Access ID (Client ID) of your Tuya Cloud Project",
    1010: "access token rejected",
    1106: "permission denied — usually the data center selected here does not match the "
          "one your Tuya Cloud Project was created in, or the project is missing its "
          "IoT Core / Smart Home Basic Service API subscription",
    2406: "the Cloud Project is not authorized in this data center — pick the data center "
          "the project was created in, or add this one to the project on iot.tuya.com",
    28841002: "access token expired",
    28841101: "the Cloud Project is missing an API subscription (IoT Core / Smart Home "
              "Basic Service) — subscribe to it on iot.tuya.com, it is free",
    28841105: "the Cloud Project is missing an API subscription (IoT Core / Smart Home "
              "Basic Service) — subscribe to it on iot.tuya.com, it is free",
}


def _api_error(code, msg: str, path: str, *, force_auth: bool = False) -> TuyaError:
    """Build the richest error we can for a failed Tuya call, and log it."""
    text = f"Tuya API error {code} on {path}: {msg}"
    hint = _CODE_HINTS.get(code)
    if hint:
        text += f" — {hint}"
    _LOGGER.debug("Tuya call failed: %s", text)
    if force_auth or code in _AUTH_CODES:
        return TuyaAuthError(text, code)
    return TuyaError(text, code)


def _headers(method, path, body_bytes, access_id, access_secret, token=""):
    t = str(int(time.time() * 1000))
    content_sha256 = hashlib.sha256(body_bytes or b"").hexdigest()
    string_to_sign = f"{method}\n{content_sha256}\n\n{path}"
    message = (access_id + token + t + string_to_sign).encode()
    sign = hmac.new(access_secret.encode(), message, hashlib.sha256).hexdigest().upper()
    h = {
        "client_id": access_id,
        "sign": sign,
        "sign_method": "HMAC-SHA256",
        "t": t,
        "lang": "en",
        "Content-Type": "application/json",
    }
    if token:
        h["access_token"] = token
    return h


def _do(host, method, path, headers, body_bytes):
    url = "https://" + host + path
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    except urllib.error.URLError as e:
        raise TuyaError(f"network error: {e}") from e
    try:
        return json.loads(raw)
    except Exception as e:
        snippet = raw[:120].decode("utf-8", "replace") if raw else "(empty)"
        raise TuyaError(f"non-JSON response from {path}: {snippet!r}") from e


class TuyaClient:
    """One Tuya Cloud Project session: self-authenticates with Access ID/Secret, then
    addresses one linked app account (UID) to list/read/control its devices."""

    def __init__(self, access_id: str, access_secret: str, uid: str,
                 region: str = DEFAULT_TUYA_REGION) -> None:
        self.access_id = access_id
        self.access_secret = access_secret
        self.uid = uid
        self.region = region if region in TUYA_REGIONS else DEFAULT_TUYA_REGION
        self.host = TUYA_REGIONS[self.region]
        self._token: str | None = None
        self._token_expiry = 0.0

    # --- transport ---
    def _call(self, method, path, body=None, _retry=True):
        body_bytes = json.dumps(body).encode() if body is not None else b""
        headers = _headers(method, path, body_bytes, self.access_id, self.access_secret,
                           self._token or "")
        r = _do(self.host, method, path, headers, body_bytes if body is not None else None)
        if r.get("success") is not True:
            code = r.get("code")
            msg = r.get("msg") or "unknown error"
            if code == 1010 and _retry:  # token invalid/expired (verified Tuya SDK constant)
                self._token = None
                self.authenticate()
                return self._call(method, path, body, _retry=False)
            raise _api_error(code, msg, path)
        return r.get("result")

    # --- auth ---
    def authenticate(self) -> None:
        """Self-authenticate this Cloud Project (Access ID/Secret only, no user password)."""
        if self._token and time.monotonic() < self._token_expiry:
            return
        body_bytes = b""
        headers = _headers("GET", "/v1.0/token?grant_type=1", body_bytes,
                           self.access_id, self.access_secret, "")
        r = _do(self.host, "GET", "/v1.0/token?grant_type=1", headers, None)
        if r.get("success") is not True:
            # A failed token request is always a project-credentials/data-center problem.
            raise _api_error(r.get("code"), r.get("msg") or "unknown error",
                             "/v1.0/token", force_auth=True)
        result = r.get("result") or {}
        self._token = result.get("access_token")
        expire_in = result.get("expire_time") or 7200
        if not self._token:
            raise TuyaAuthError("Tuya token response missing access_token")
        self._token_expiry = time.monotonic() + expire_in - TOKEN_TTL_SAFETY

    def list_devices(self) -> list[dict]:
        """Devices bound to the linked ILIFE Clean account (self.uid)."""
        self.authenticate()
        if not self.uid:
            raise TuyaAuthError("no UID configured — link the ILIFE Clean account to the "
                                "Tuya Cloud Project first (see README)")
        devices = self._call("GET", f"/v1.0/users/{urllib.parse.quote(self.uid)}/devices")
        if not devices:
            raise TuyaNoDevicesError(
                "no devices found for this UID — check that the UID is correct and the "
                "ILIFE Clean account has been linked to this Cloud Project (see README)")
        return devices

    def device_detail(self, device_id: str) -> dict:
        self.authenticate()
        return self._call("GET", f"/v1.0/devices/{urllib.parse.quote(device_id)}") or {}

    def device_specification(self, device_id: str) -> dict:
        """Function + status-range schema actually advertised by this device."""
        self.authenticate()
        return self._call(
            "GET", f"/v1.0/devices/{urllib.parse.quote(device_id)}/specifications") or {}

    def send_commands(self, device_id: str, commands: list[dict]) -> bool:
        self.authenticate()
        try:
            self._call("POST", f"/v1.0/devices/{urllib.parse.quote(device_id)}/commands",
                       {"commands": commands})
        except TuyaError:
            detail = self.device_detail(device_id)
            if detail.get("online") is False:
                raise TuyaOfflineError(f"device offline: commands {commands}") from None
            raise
        return True


class TuyaVacuum:
    """One bound device, addressed via a TuyaClient. Mirrors api.ILifeDevice's role."""

    def __init__(self, client: TuyaClient, device: dict) -> None:
        self.client = client
        self.device = device
        self.device_id = device["id"]

    def detail(self) -> dict:
        return self.client.device_detail(self.device_id)

    def specification(self) -> dict:
        return self.client.device_specification(self.device_id)

    def send(self, code: str, value) -> bool:
        return self.client.send_commands(self.device_id, [{"code": code, "value": value}])

    def send_many(self, commands: list[dict]) -> bool:
        return self.client.send_commands(self.device_id, commands)
