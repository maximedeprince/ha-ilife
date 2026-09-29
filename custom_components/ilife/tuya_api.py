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

import datetime
import hashlib
import hmac
import json
import logging
import os.path
import time
import urllib.error
import urllib.parse
import urllib.request

_LOGGER = logging.getLogger(__name__)

# Tuya Cloud data-center endpoints (developer.tuya.com data-center reference). All six
# must be offered: a Cloud Project can only be reached on the data center it was created
# in, and Tuya routes several countries (France among them) to either European DC, so
# omitting Western Europe / Eastern America made those projects simply unreachable.
# Keys "eu"/"us"/"cn"/"in" are kept as-is so existing config entries keep working.
TUYA_REGIONS = {
    "eu": "openapi.tuyaeu.com",          # Central Europe
    "weu": "openapi-weaz.tuyaeu.com",    # Western Europe
    "us": "openapi.tuyaus.com",          # Western America
    "eus": "openapi-ueaz.tuyaus.com",    # Eastern America
    "cn": "openapi.tuyacn.com",          # China
    "in": "openapi.tuyain.com",          # India
}

# Shown in the config flow — the raw keys mean nothing to a user picking a data center
# from the Tuya console, which names them in full.
TUYA_REGION_LABELS = {
    "eu": "Central Europe",
    "weu": "Western Europe",
    "us": "Western America",
    "eus": "Eastern America",
    "cn": "China",
    "in": "India",
}

DEFAULT_TUYA_REGION = "eu"

TOKEN_TTL_SAFETY = 60  # seconds; refresh this long before actual expiry
MAX_MAP_FILE_SIZE = 8 * 1024 * 1024


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
_AUTH_CODES = {1004, 1005, 1010, 1013, 1106, 2406, 28841002, 28841101, 28841105}

# The setup mistakes users actually hit (see issues #3 and #13): a bare "Tuya API error
# (1106): permission deny" tells nobody anything, so each known code gets the fix.
_CODE_HINTS: dict[int, str] = {
    1004: "signature rejected — check the Access Secret was pasted in full (no stray "
          "spaces), and that the Home Assistant host clock is correct",
    1005: "unknown Access ID — check the Access ID (Client ID) of your Tuya Cloud Project",
    1010: "access token rejected",
    1013: "the request timestamp was rejected — the Home Assistant host clock is out of "
          "sync (Tuya signs every request with the current time and allows only a few "
          "minutes of drift). Fix time synchronisation (NTP) on the host",
    1106: "permission denied — usually the data center selected here does not match the "
          "one your Tuya Cloud Project was created in, or the project is missing its "
          "IoT Core / Smart Home Basic Service API subscription",
    2406: "the Cloud Project is not authorized in this data center — pick the data center "
          "the project was created in, or add this one to the project on iot.tuya.com",
    # Tuya returns this for an expired Cloud Project trial as well as for a rejected
    # token, and its own message says which — so the hint must not contradict it.
    28841002: "the Cloud Project's free trial has expired, or the token was rejected. "
              "Tuya's Trial Edition is time-limited but renewable at no cost: on "
              "iot.tuya.com go to Cloud \u2192 Cloud Services \u2192 IoT Core, renew the "
              "trial, then re-authorize it for your project under the project's Service "
              "API tab",
    28841101: "the Cloud Project is missing an API subscription (IoT Core / Smart Home "
              "Basic Service) — subscribe to it on iot.tuya.com, it is free",
    28841105: "the Cloud Project is missing an API subscription (IoT Core / Smart Home "
              "Basic Service) — subscribe to it on iot.tuya.com, it is free",
}


def _format_skew(seconds: float) -> str:
    """Human-readable clock offset — minutes matter more than seconds at this scale."""
    seconds = abs(seconds)
    if seconds < 120:
        return f"{seconds:.0f} seconds"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 120:
        return f"{minutes} min {secs} s"
    return f"{minutes // 60} h {minutes % 60} min"


def _clock_skew_note(server_t) -> str:
    """Tuya stamps every response — errors included — with its own clock in `t`, so a
    request rejected for a bad timestamp can name the actual offset rather than leaving
    the user guessing. Round-trip latency is milliseconds, so anything above a few
    seconds is genuine host drift.

    The offset is reported, never silently applied to the signature: a host clock this
    far off also breaks Home Assistant's own scheduling, history and recorder, so the
    fix belongs on the host, not hidden in here.
    """
    try:
        skew = time.time() - float(server_t) / 1000.0
    except (TypeError, ValueError):
        return ""
    if abs(skew) < 5:
        return ""
    direction = "ahead of" if skew > 0 else "behind"
    return (f"Measured against Tuya's own clock, this Home Assistant host is "
            f"{_format_skew(skew)} {direction} the server.")


def _api_error(code, msg: str, path: str, *, server_t=None,
               force_auth: bool = False) -> TuyaError:
    """Build the richest error we can for a failed Tuya call, and log it."""
    text = f"Tuya API error {code} on {path}: {msg}"
    hint = _CODE_HINTS.get(code)
    if hint:
        text += f" — {hint}"
    note = _clock_skew_note(server_t)
    if note:
        text += f". {note}"
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


def parse_record_extend(extend: object) -> dict | None:
    """What a stored map record's `extend` says about its clean, or None.

    "00008_20260927_215313_002_003_04379_00710_00003": map number, the day and
    wall-clock time (robot local) the clean started, area in m², duration in
    minutes. The last three fields are not understood.
    """
    fields = str(extend or "").split("_")
    if len(fields) < 5 or not all(field.isdigit() for field in fields[1:5]):
        return None
    try:
        started = datetime.datetime.strptime(fields[1] + fields[2], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return {"started": started, "area": int(fields[3]), "duration": int(fields[4])}


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
            raise _api_error(code, msg, path, server_t=r.get("t"))
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
                             "/v1.0/token", server_t=r.get("t"), force_auth=True)
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
        # Logged before the call: when a command is accepted by Tuya and the robot still
        # does nothing, the only thing worth knowing is exactly what went out (#24).
        _LOGGER.debug("Tuya command -> %s: %s", device_id, commands)
        try:
            self._call("POST", f"/v1.0/devices/{urllib.parse.quote(device_id)}/commands",
                       {"commands": commands})
        except TuyaError:
            detail = self.device_detail(device_id)
            if detail.get("online") is False:
                raise TuyaOfflineError(f"device offline: commands {commands}") from None
            raise
        return True

    def device_properties(self, device_id: str) -> dict:
        """Every product DP by code, including those outside the standard instruction
        set (room names, room cleaning). {code: value}."""
        self.authenticate()
        result = self._call(
            "GET", f"/v2.0/cloud/thing/{urllib.parse.quote(device_id)}/shadow/properties"
        ) or {}
        if not isinstance(result, dict) or not isinstance(result.get("properties") or [], list):
            raise TuyaError("unexpected shadow properties response")
        return {
            item["code"]: item.get("value")
            for item in result.get("properties") or []
            if isinstance(item, dict) and item.get("code")
        }

    def device_model(self, device_id: str) -> dict:
        """The product's thing model: {code: {"type", "access", "range"|"min"|"max"...}}.

        Wider than /specifications, which only covers the standard instruction set:
        on the A30 Pro it is the one place that says suction and water accept
        "closed", and what `cleaning_efficiency` can be set to.
        """
        self.authenticate()
        result = self._call(
            "GET", f"/v2.0/cloud/thing/{urllib.parse.quote(device_id)}/model"
        ) or {}
        if not isinstance(result, dict):
            raise TuyaError("unexpected thing model response")
        try:
            model = json.loads(result.get("model") or "{}")
        except (TypeError, ValueError) as err:
            raise TuyaError("unexpected thing model response") from err
        if not isinstance(model, dict):
            raise TuyaError("unexpected thing model response")
        out = {}
        for service in model.get("services") or []:
            if not isinstance(service, dict):
                continue
            for prop in service.get("properties") or []:
                if not isinstance(prop, dict):
                    continue
                code = prop.get("code")
                if code:
                    out[code] = {
                        **(prop.get("typeSpec") or {}),
                        "access": prop.get("accessMode"),
                    }
        return out

    def issue_properties(self, device_id: str, properties: dict) -> None:
        """Write product DPs by code — the way to reach non-standard DPs."""
        self.authenticate()
        _LOGGER.debug("Tuya properties -> %s: %s", device_id, properties)
        self._call(
            "POST",
            f"/v2.0/cloud/thing/{urllib.parse.quote(device_id)}/shadow/properties/issue",
            {"properties": json.dumps(properties)},
        )

    def _sweeper_endpoint(self, device_id: str, action: str) -> str:
        return f"/v1.0/users/sweepers/file/{urllib.parse.quote(device_id, safe='')}/{action}"

    def _sweeper_call(self, path: str):
        """GET a Robot Vacuum Open API endpoint, naming the missing service on 1106."""
        try:
            return self._call("GET", path)
        except TuyaError as err:
            if err.code == 1106:
                raise TuyaError(
                    f"{err}. For map access, also authorize the Robot Vacuum Open APIs "
                    "service for this Tuya Cloud Project",
                    err.code,
                ) from err
            raise

    def realtime_map_files(self, device_id: str) -> list[dict]:
        """Download the map files of the run in progress (empty when there is none).

        Not every model publishes these: the A30 Pro only stores a map once a run
        has finished, see `latest_stored_map`.
        """
        self.authenticate()
        links = self._sweeper_call(self._sweeper_endpoint(device_id, "realtime-map")) or []
        if not isinstance(links, list):
            raise TuyaError("unexpected realtime-map response: result is not a list")
        files = self._download_map_files(links, source="realtime")
        _LOGGER.debug(
            "Tuya realtime-map for %s: %s",
            device_id,
            [(item["map_type"], len(item["payload"]), item["payload"][:24].hex())
             for item in files] or "no files",
        )
        return files

    def stored_map_records(self, device_id: str, count: int = 5) -> list[dict]:
        """Stored map records (`id`, `time`, `extend`), newest first. Models that
        store a map per clean (A30 Pro) make this their cleaning history."""
        self.authenticate()
        params = urllib.parse.urlencode(
            {"file_type": "pic", "page_no": 1, "page_size": count})
        listing = self._sweeper_call(
            f"{self._sweeper_endpoint(device_id, 'list')}?{params}") or {}
        if not isinstance(listing, dict):
            raise TuyaError("unexpected map list response: result is not an object")
        records = [
            item for item in listing.get("datas") or []
            if isinstance(item, dict) and item.get("id")
        ]

        def _record_time(item: dict) -> int:
            try:
                return int(item.get("time") or 0)
            except (TypeError, ValueError):
                return 0

        return sorted(records, key=_record_time, reverse=True)

    def latest_stored_map(self, device_id: str) -> dict | None:
        """The newest stored map record (`id`, `time`, ...), or None if there is none."""
        records = self.stored_map_records(device_id)
        return records[0] if records else None

    def stored_map_files(self, device_id: str, record: dict) -> list[dict]:
        """Download the files of one stored map record from `latest_stored_map`."""
        self.authenticate()
        params = urllib.parse.urlencode({"id": record["id"]})
        download = self._sweeper_call(
            f"{self._sweeper_endpoint(device_id, 'download')}?{params}") or {}
        if not isinstance(download, dict):
            raise TuyaError("unexpected map download response: result is not an object")
        links = [
            {"map_type": map_type, "map_url": download.get(role), "role": role}
            for role, map_type in (("app_map", 0), ("robot_map", 1))
            if download.get(role)
        ]
        return self._download_map_files(links, source="stored", record=record)

    @staticmethod
    def _download_map_files(
        links: list, *, source: str, record: dict | None = None
    ) -> list[dict]:
        """Fetch each signed map URL. The URLs themselves are never returned or logged."""
        files = []
        for item in links:
            if not isinstance(item, dict):
                continue
            url = item.get("map_url")
            if not isinstance(url, str) or not url:
                continue
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise TuyaError("map API returned a non-HTTPS download URL")
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "HomeAssistant/ILIFE",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read(MAX_MAP_FILE_SIZE + 1)
            except (urllib.error.HTTPError, urllib.error.URLError) as err:
                raise TuyaError(f"map file download failed: {err}") from err
            if len(payload) > MAX_MAP_FILE_SIZE:
                raise TuyaError(f"map file exceeds the {MAX_MAP_FILE_SIZE}-byte limit")
            files.append({
                "map_type": item.get("map_type"),
                "role": item.get("role"),
                "source": source,
                "record_id": record.get("id") if record else None,
                "record_time": record.get("time") if record else None,
                "filename": os.path.basename(urllib.parse.unquote(parsed.path)) or None,
                "payload": payload,
            })
        return files


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

    def properties(self) -> dict:
        return self.client.device_properties(self.device_id)

    def model(self) -> dict:
        return self.client.device_model(self.device_id)

    def issue_properties(self, properties: dict) -> None:
        self.client.issue_properties(self.device_id, properties)

    def realtime_map_files(self) -> list[dict]:
        return self.client.realtime_map_files(self.device_id)

    def latest_stored_map(self) -> dict | None:
        return self.client.latest_stored_map(self.device_id)

    def stored_map_records(self, count: int = 5) -> list[dict]:
        return self.client.stored_map_records(self.device_id, count)

    def stored_map_files(self, record: dict) -> list[dict]:
        return self.client.stored_map_files(self.device_id, record)
