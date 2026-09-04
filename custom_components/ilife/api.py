"""Low-level ILIFE client — Alibaba IoT cloud API (API Gateway HMAC-SHA1 signing).

Synchronous (urllib): call from HA via hass.async_add_executor_job.
The static app secrets below are the ILIFE app's own shared secrets (identical for every
ILIFE user), so they are safe to ship. Split in two layers:
  * ILifeAccount : email/password session (login, token, re-auth, device listing).
  * ILifeDevice  : one bound vacuum, delegating signed calls to its account.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .brands import Brand, get_brand

_LOGGER = logging.getLogger(__name__)

# Shared across all whitelabels (OpenAccount SDK-level constants). Per-brand values
# (appKey/appSecret/appID/appVersion/region) live in brands.py.
OA_HOST = "sdk.openaccount.aliyun.com"
UTDID = "amDvkNT/EMgDAN2WcKz0yYap"
RSA_PUB = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAl4EFDk91/ArPHjyX7UBzofPTAD3pcP8FMgOs83hvL"
    "EcbFJOVASrPAjbJTuXsSZJd9tYPwKbuqlGqndvdl2Kn2zLFpLOcFAYOyaIDFzDOCWQw/kMjcm1U08BvPE7db"
    "tkGM23lCyTBlDMHWJvUz3JVTZm6ApGWEOGRhs1rECjcS9HXttnllQ2gTtBAW5Xjb8tzDgWR0jMaHzduCcSim"
    "HPtQO4Osh4Op3ianRocbb9o/4OR8HgKdbaKO3Sq2+pYV7FveXmfXqUr5lH7oHji+4j5TaU4WXRGKOjHSVXtN"
    "0UrfCXtsWE0aGCXXQN78NJUf5VrJMh14mqiSrR07wgu3UG7OwIDAQAB"
)

# Alibaba Living Link regions (accounts/devices are partitioned by region).
# EU is the tested region; the others follow Alibaba's standard host naming.
REGIONS = {
    "eu": ("eu-central-1.api-iot.aliyuncs.com", "living-account.eu-central-1.aliyuncs.com"),
    "us": ("us-east-1.api-iot.aliyuncs.com", "living-account.us-east-1.aliyuncs.com"),
    "ap": ("ap-southeast-1.api-iot.aliyuncs.com", "living-account.ap-southeast-1.aliyuncs.com"),
    "cn": ("cn-shanghai.api-iot.aliyuncs.com", "living-account.cn-shanghai.aliyuncs.com"),
}
DEFAULT_REGION = "eu"
STATUS_TTL = 45  # seconds; shared online-status cache at account level


class ILifeError(Exception):
    """Generic error."""


class ILifeAuthError(ILifeError):
    """Invalid credentials."""


class ILifeOfflineError(ILifeError):
    """Device unreachable from the cloud (asleep at the dock) — command not delivered."""


# --------------------------------------------------------------------------- #
#  Map decoding helpers (CleanMapData = 2-bit-per-pixel bitmap, tiled in packs)
# --------------------------------------------------------------------------- #
def _map_fill(cm):
    if not cm:
        return 0
    try:
        return sum(bin(x).count("1") for x in base64.b64decode(cm)[2:])
    except Exception:
        return 0


def _decode_grid(cm):
    """CleanMapData (2bpp bitmap) -> (width, height, 2D grid of 0/1/2/3)."""
    b = base64.b64decode(cm)
    if len(b) < 5 or b[0] != 0x01:
        raise ValueError("unexpected format")
    bpr = b[1]
    data = b[2:]
    rows = len(data) // bpr
    g = []
    for r in range(rows):
        chunk = data[r * bpr:(r + 1) * bpr]
        row = []
        for byte in chunk:
            for p in range(4):
                row.append((byte >> ((3 - p) * 2)) & 0x3)
        g.append(row)
    return bpr * 4, rows, g


def _assemble_maps(packs):
    """A clean's map is split into PACKS (tiles): CleanHistory returns PackNum items with
    PackId=1..PackNum, each a horizontal band. Stack them vertically in PackId order to
    rebuild the full map. `packs` = {PackId(int): CleanMapData}. Returns one CleanMapData."""
    if not packs:
        return None
    ids = sorted(packs, key=lambda k: (k is None, k))
    grids = []
    for pid in ids:
        try:
            grids.append(_decode_grid(packs[pid]))
        except Exception:
            continue
    if not grids:
        return None
    if len(grids) == 1:
        return packs[ids[0]]
    w = max(g[0] for g in grids)
    rows = []
    for _gw, _gh, g in grids:
        for row in g:
            rows.append(row + [0] * (w - len(row)) if len(row) < w else row)
    bpr = w // 4
    out = bytearray([0x01, bpr])
    for row in rows:
        for bx in range(bpr):
            byte = 0
            for p in range(4):
                byte |= (row[bx * 4 + p] & 0x3) << ((3 - p) * 2)
            out.append(byte)
    return base64.b64encode(bytes(out)).decode()


# --------------------------------------------------------------------------- #
#  Signing / transport
# --------------------------------------------------------------------------- #
def _rsa_encrypt_pw(pw: str) -> str:
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    key = load_der_public_key(base64.b64decode(RSA_PUB))
    return base64.b64encode(key.encrypt(pw.encode(), padding.PKCS1v15())).decode()


def _headers(brand: Brand, accept, ctype, content_md5, sign_url, extra=None):
    nonce = str(uuid.uuid4())
    ts = str(int(time.time() * 1000))
    date = email.utils.formatdate(usegmt=True)
    signed = {"x-ca-key": brand.appkey, "x-ca-nonce": nonce,
              "x-ca-signature-method": "HmacSHA1", "x-ca-timestamp": ts}
    hstr = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    sts = f"POST\n{accept}\n{content_md5}\n{ctype}\n{date}\n{hstr}{sign_url}"
    sig = base64.b64encode(hmac.new(brand.appsecret, sts.encode(), hashlib.sha1).digest()).decode()
    h = {"date": date, "x-ca-signature": sig, "x-ca-nonce": nonce, "x-ca-key": brand.appkey,
         "ca_version": "1", "accept": accept, "x-ca-timestamp": ts,
         "x-ca-signature-headers": "x-ca-nonce,x-ca-timestamp,x-ca-key,x-ca-signature-method",
         "x-ca-signature-method": "HmacSHA1", "user-agent": "ALIYUN-ANDROID-DEMO",
         "content-type": ctype}
    if content_md5:
        h["content-md5"] = content_md5
    if extra:
        h.update(extra)
    return h


def _do(host, path, headers, body):
    req = urllib.request.Request("https://" + host + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        xca = {k: v for k, v in e.headers.items() if k.lower().startswith("x-ca-error")}
        raw = e.read()
        try:
            return json.loads(raw)
        except Exception:
            return {"httperror": e.code, "xca": xca, "body": raw[:200].decode("utf-8", "replace")}
    except urllib.error.URLError as e:
        raise ILifeError(f"network error: {e}") from e
    try:
        return json.loads(raw)
    except Exception as e:
        snippet = raw[:120].decode("utf-8", "replace") if raw else "(empty)"
        raise ILifeError(f"non-JSON response from {path}: {snippet!r}") from e


def _post_form(brand: Brand, host, path, form, extra=None):
    accept = "application/json; charset=utf-8"
    ctype = "application/x-www-form-urlencoded; charset=utf-8"
    sign_url = path + "?" + "&".join(f"{k}={form[k]}" for k in sorted(form))
    h = _headers(brand, accept, ctype, "", sign_url, extra)
    body = "&".join(f"{k}={urllib.parse.quote(str(form[k]), safe='')}" for k in sorted(form)).encode()
    return _do(host, path, h, body)


def _post_iot(brand: Brand, host, path, d, token=None, api_ver="1.0.2"):
    rid = str(uuid.uuid4())
    c = {"apiVer": api_ver, "language": "en-US"}
    if token:
        c["iotToken"] = token
    body = json.dumps({"a": rid, "b": "1.0", "c": c, "d": d, "id": rid,
                       "params": {"$ref": "$.d"}, "request": {"$ref": "$.c"}, "version": "1.0"}).encode()
    cmd5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    full_path = path + "?x-ca-request-id=" + rid
    h = _headers(brand, "application/json; charset=utf-8", "application/octet-stream; charset=utf-8", cmd5, full_path)
    return _do(host, full_path, h, body)


# --------------------------------------------------------------------------- #
#  Account (session) layer
# --------------------------------------------------------------------------- #
class ILifeAccount:
    """One ILIFE account session: login, shared iotToken, automatic re-auth,
    device listing and a short-lived online-status cache shared by all devices."""

    def __init__(self, email_addr: str, password: str, region: str | None = None,
                 brand: str = "ilife") -> None:
        self._email = email_addr.strip()
        self._password = password
        self.brand: Brand = get_brand(brand)
        region = region or self.brand.default_region
        self.region = region if region in REGIONS else self.brand.default_region
        self.api_host, self.login_host = REGIONS[self.region]
        self.token: str | None = None
        self._lock = threading.Lock()
        self._status: dict[str, bool] = {}
        self._status_ts = 0.0

    # --- auth ---
    def login(self) -> list[dict]:
        """Authenticate and return the full list of bound devices."""
        ctx = {"context": {"sdkVersion": self.brand.sdk_version, "utDid": UTDID, "platformName": "android",
                           "netType": "wifi", "appKey": self.brand.appkey, "yunOSId": "",
                           "appVersion": self.brand.app_version,
                           "appAuthToken": "", "securityToken": ""},
                "config": {"version": 0, "lastModify": 0},
                "device": {"model": "sdk_gphone_x86_64", "brand": "goldfish_x86_64", "platformVersion": "30"}}
        vid = device_id = None
        r = {}
        for attempt in range(4):
            r = _post_form(self.brand, OA_HOST, "/api/prd/connect.json", {"request": json.dumps(ctx)})
            data = r.get("data", {})
            vid = data.get("vid")
            device_id = (((data.get("data") or {}).get("device") or {}).get("data") or {}).get("deviceId")
            if vid and device_id:
                break
            _LOGGER.debug("ILIFE connect attempt %d failed", attempt + 1)
            time.sleep(2)
        if not vid or not device_id:
            raise ILifeError("connect handshake failed after 4 attempts")
        login_req = {"password": _rsa_encrypt_pw(self._password), "loginId": self._email,
                     "riskControlInfo": {"appVersion": "123", "USE_OA_PWD_ENCRYPT": "true",
                        "utdid": "ffffffffffffffffffffffff", "netType": "wifi", "umidToken": "",
                        "locale": "en_US", "appVersionName": self.brand.app_version, "deviceId": device_id,
                        "routerMac": "02:00:00:00:00:00", "platformVersion": "30", "appAuthToken": "",
                        "appID": self.brand.oa_app_id, "signType": "RSA", "sdkVersion": self.brand.sdk_version,
                        "model": "sdk_gphone_x86_64", "USE_H5_NC": "true", "platformName": "android",
                        "brand": "google", "yunOSId": ""}}
        r = _post_form(self.brand, self.login_host, "/api/prd/login.json",
                       {"loginRequest": json.dumps(login_req)}, extra={"vid": vid})
        ld = r.get("data", {})
        if ld.get("code") != 1:
            # Surface the server's own reason. A login that is valid in the app can
            # still be refused here by Alibaba OA (risk-control / captcha step,
            # locked account, region mismatch...). Logged at warning so it shows up
            # in the HA log without any extra debug configuration -- previously this
            # path was silent and the user only ever saw a generic "invalid_auth".
            _LOGGER.warning("ILIFE login refused by server: code=%s message=%s",
                            ld.get("code"), ld.get("message"))
            _LOGGER.debug("ILIFE login.json response: %s", r)
            raise ILifeAuthError(
                f"login refused (code {ld.get('code')}): "
                f"{ld.get('message') or 'invalid credentials'}")
        sid = ld.get("data", {}).get("loginSuccessResult", {}).get("sid")
        r = _post_iot(self.brand, self.api_host, "/account/createSessionByAuthCode",
                      {"request": {"authCode": sid, "accountType": "OA_SESSION", "appKey": self.brand.appkey}},
                      api_ver="1.0.4")
        if r.get("code") != 200:
            _LOGGER.debug("ILIFE createSession failed: %s", r)
            raise ILifeError(f"session creation failed (code {r.get('code')})")
        self.token = r["data"]["iotToken"]
        return self.list_devices()

    def _iot(self, path, d, api_ver="1.0.2", _retry=True):
        r = _post_iot(self.brand, self.api_host, path, d, token=self.token, api_ver=api_ver)
        if r.get("code") != 200 and _retry:
            stale = self.token
            with self._lock:  # one re-login even if several device calls fail at once
                if self.token == stale:
                    self.login()
            return self._iot(path, d, api_ver, _retry=False)
        return r

    def list_devices(self) -> list[dict]:
        r = self._iot("/uc/listBindingByAccount", {}, _retry=False)
        devs = (r.get("data") or {}).get("data") or []
        if not devs:
            raise ILifeError("no device bound to this account")
        # refresh the shared status cache while we have the list
        self._status = {dv.get("iotId"): dv.get("status") == 1 for dv in devs}
        self._status_ts = time.monotonic()
        return devs

    def device_status(self) -> dict[str, bool]:
        """{iotId: online} — cached ~STATUS_TTL s, one call for all devices."""
        if time.monotonic() - self._status_ts < STATUS_TTL and self._status:
            return self._status
        r = self._iot("/uc/listBindingByAccount", {}, _retry=False)
        if r.get("code") == 200:
            self._status = {dv.get("iotId"): dv.get("status") == 1
                            for dv in (r.get("data") or {}).get("data") or []}
            self._status_ts = time.monotonic()
        return self._status


# --------------------------------------------------------------------------- #
#  Device layer
# --------------------------------------------------------------------------- #
class ILifeDevice:
    """One bound vacuum. Signed calls are delegated to its ILifeAccount (shared token)."""

    def __init__(self, account: ILifeAccount, device: dict) -> None:
        self.account = account
        self.device = device
        self.iot_id = device["iotId"]

    def _iot(self, path, d, api_ver="1.0.2"):
        return self.account._iot(path, d, api_ver)

    # --- state & commands ---
    def get_state(self):
        r = self._iot("/thing/properties/get", {"iotId": self.iot_id, "tag": 0})
        if r.get("code") != 200:
            _LOGGER.debug("ILIFE get_state failed: %s", r)
            raise ILifeError(f"get_state failed (code {r.get('code')})")
        out = {}
        for k, v in (r.get("data") or {}).items():
            out[k] = v.get("value") if isinstance(v, dict) else v
        return out

    def set_prop(self, name, value, tag=None, key=True):
        d = {"iotId": self.iot_id, "items": {name: value}}
        if key:
            d["key"] = name
        if tag is not None:
            d["tag"] = str(tag)
        r = self._iot("/thing/properties/set", d)
        code = r.get("code")
        if code != 200:
            _LOGGER.debug("ILIFE set %s failed: %s", name, r)
            # 9201 = Alibaba IoT "device offline": the vacuum drops its cloud link
            # while asleep at the dock, so on-demand commands can't be delivered.
            if code in (9201, "9201"):
                raise ILifeOfflineError(f"set {name} failed: device offline (code {code})")
            raise ILifeError(f"set {name} failed (code {code})")
        return True

    def set_schedule(self, n, struct):
        d = {"iotId": self.iot_id, "items": {f"Schedule{n}": struct}, "position": n}
        r = self._iot("/thing/properties/set", d)
        if r.get("code") != 200:
            _LOGGER.debug("ILIFE set Schedule%s failed: %s", n, r)
            raise ILifeError(f"set schedule failed (code {r.get('code')})")
        return True

    def clean_history(self, limit=20):
        """Cleaning history via the timeline endpoint (apiVer 1.0.0).
        Each clean's map is tiled into PackNum packs (PackId=1..N); we group by StartTime,
        collect the packs and stack them into one complete map. Recent first."""
        d = {"identifier": "CleanHistory", "iotId": self.iot_id, "start": 0,
             "limit": 200, "end": int(time.time() * 1000), "order": "desc"}
        r = self._iot("/living/device/property/timeline/get", d, api_ver="1.0.0")
        if r.get("code") != 200:
            _LOGGER.debug("ILIFE clean_history failed: %s", r)
            raise ILifeError(f"clean_history failed (code {r.get('code')})")
        groups = {}
        for it in (r.get("data") or {}).get("items") or []:
            raw = it.get("data")
            try:
                cd = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                continue
            start = cd.get("StartTime")
            g = groups.get(start)
            if g is None:
                g = groups[start] = {"start": start, "area": 0, "duration": 0,
                                     "reason": cd.get("StopCleanReason"), "packs": {}}
            area = round((cd.get("CleanTotalArea") or 0) / 100, 1)
            dur = round((cd.get("CleanTotalTime") or 0) / 60)
            if area > g["area"]:
                g["area"] = area
            if dur > g["duration"]:
                g["duration"] = dur
            cm = cd.get("CleanMapData")
            if cm:
                pid = cd.get("PackId")
                g["packs"][pid if pid is not None else 1] = cm
        out = []
        for start in sorted(groups, key=lambda s: s or 0, reverse=True)[:limit]:
            g = groups[start]
            out.append({"start": g["start"], "area": g["area"], "duration": g["duration"],
                        "reason": g["reason"], "map": _assemble_maps(g["packs"])})
        return out

    def is_online(self):
        """True/False/None from the account-level shared status cache."""
        return self.account.device_status().get(self.iot_id)

    def work_mode(self, mode):
        return self.set_prop("WorkMode", mode, mode)

    def clean_direction(self, direction):
        return self.set_prop("CleanDirection", direction, direction)

    def reset_part(self, field, current):
        """Reset a consumable life counter to 100% (new). Mirrors the app: the full
        PartsStatus struct is sent (tag 7, no key), the target field set to 100 and
        the two others preserved at their current values."""
        struct = {k: (current or {}).get(k, 100)
                  for k in ("FilterLife", "SideBrushLife", "MainBrushLife")}
        struct[field] = 100
        d = {"iotId": self.iot_id, "tag": 7, "items": {"PartsStatus": struct}}
        r = self._iot("/thing/properties/set", d)
        code = r.get("code")
        if code != 200:
            _LOGGER.debug("ILIFE reset %s failed: %s", field, r)
            if code in (9201, "9201"):
                raise ILifeOfflineError(f"reset {field} failed: device offline (code {code})")
            raise ILifeError(f"reset {field} failed (code {code})")
        return True
