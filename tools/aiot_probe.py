#!/usr/bin/env python3
"""Probe the 3irobotix AIoT cloud from your own account (#26, #37).

The ILIFE T60s, the 3i P10 Ultra and Kärcher's robots live on 3irobotix's own IoT
cloud ("AIoT"), which is neither Alibaba Living Link nor Tuya. This script is not part
of the integration: it checks, step by step, what a backend for that cloud needs, and
prints only what is safe to paste on a public issue. Serial numbers, MAC and IP
addresses, user ids, tokens, signatures, nicknames and long blobs (maps) are masked.

    pip install cryptography paho-mqtt

    # 1. Reach the cloud and list its hosts (no account needed). This checks the
    #    signing and the encryption, not the tenant: domains/list answers any tenantId.
    python3 aiot_probe.py domains --tenant 1534051142064500736 --project android_iot.3i

    # 2. Log in and list the robots (the password is asked for, never echoed). This is
    #    where a wrong tenantId or project type shows: the login is refused.
    python3 aiot_probe.py login --tenant 1534051142064500736 --project android_iot.3i \\
        --region eu --account you@example.com

    # 3. The same, then listen to the robots for 10 minutes: start a clean in the app
    python3 aiot_probe.py login ... --listen 600

The protocol is the one python-karcher (MIT, https://github.com/lafriks/python-karcher)
documents for Kärcher: requests are signed with md5(token + ts + nonce + data), the
account and password are sent AES-128-ECB encrypted with md5(tenantId)[8:24], and the
robots talk MQTT on the broker the domain list names.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.client
import json
import random
import ssl
import string
import sys
import threading
import time
import urllib.parse

API_HOSTS = {
    "eu": "eu-appaiot.3irobotix.net",
    "us": "us-appaiot.3irobotix.net",
    "cn": "cn-appaiot.3irobotix.net",
}
# The API's certificate is self-signed (CN *.3irobotix.net): the apps pin it, and so
# does this script, instead of trusting any certificate.
PINNED_SHA256 = "68a368925db3de516a8064fd7038a34945d86edf113308662c8785a4c9f54a10"
LOGIN_PATH = "/user-center/auth/login"
DEVICES_PATH = "/smart-home-service/smartHome/user/getDeviceInfoByUserId/{user_id}"
# What python-karcher asks a Kärcher robot for; another model answers what it knows.
KARCHER_PROPERTIES = [
    "status", "firmware_code", "firmware", "fault", "mode", "wind", "water",
    "repeat_state", "charge_state", "quantity", "work_mode", "sweep_type", "build_map",
    "cleaning_area", "cleaning_time", "current_map_id", "custom_type", "privacy",
    "alarm", "volume", "tank_state", "cloth_state", "mop_route", "map_num",
    "language", "voice_type", "quiet_status", "quiet_is_open",
]
MQTT_TOPICS = [
    "thing/event/property/post", "thing/service/property/set_reply",
    "thing/service/property/get_reply", "thing/service_invoke",
    "thing/service_invoke_reply/#", "thing/event/cur_path/post",
    "ota/service/upgrade/post", "ota/service/version/post",
]

# Masked wherever they appear, at any depth (compared lower-case, without _ and -).
_SECRET_KEYS = {
    "sn", "mac", "ip", "ssid", "bssid", "id", "userid", "uid", "deviceid", "devid",
    "auth", "token", "emqtoken", "authorization", "sign", "password", "username",
    "account", "email", "phone", "mobile", "nickname", "nick", "name", "lat", "lng",
    "lon", "latitude", "longitude", "address", "registerid", "android", "andipad",
    "wifiname", "routername", "ownerid", "homeid", "roomid", "familyid",
    "countrycity", "country", "city", "continent", "timezone",
}
_LONG = 48  # longer strings are blobs (maps, paths, certificates): length only


def _norm(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def redact(value, key: str = ""):
    """A copy of `value` that is safe to post: secrets masked, blobs summarized."""
    k = _norm(key)
    if k in _SECRET_KEYS or "token" in k or "secret" in k or "passw" in k:
        return "<masked>" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return {key2: redact(v, str(key2)) for key2, v in value.items()}
    if isinstance(value, list):
        if len(value) > 12 and all(not isinstance(v, (dict, list)) for v in value):
            return f"<list of {len(value)}>"
        return [redact(v, key) for v in value]
    if isinstance(value, str) and len(value) > _LONG:
        return f"<{len(value)} chars>"
    return value


def show(title: str, value) -> None:
    print(f"\n== {title}")
    print(json.dumps(redact(value), indent=2, ensure_ascii=False))


def _aes_key(tenant: str) -> bytes:
    return hashlib.md5(tenant.encode()).hexdigest()[8:24].encode()


def encrypt(tenant: str, text: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = text.encode()
    pad = 16 - len(raw) % 16
    enc = Cipher(algorithms.AES(_aes_key(tenant)), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(raw + bytes([pad]) * pad) + enc.finalize()).decode()


def decrypt(tenant: str, data: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    dec = Cipher(algorithms.AES(_aes_key(tenant)), modes.ECB()).decryptor()
    raw = dec.update(base64.b64decode(data)) + dec.finalize()
    if not raw or not 1 <= raw[-1] <= 16:
        raise ValueError("bad padding")
    return raw[: -raw[-1]].decode()


def _sign_data(body: dict) -> str:
    """POST data as the cloud signs it: keys and values run together, in body order."""
    out = ""
    for key, value in body.items():
        out += key
        if value is None:
            out += "null"
        elif isinstance(value, str):
            out += value
        elif isinstance(value, (dict, list, bool)):
            out += json.dumps(value, separators=(",", ":"))
        else:
            out += str(value)
    return out


class Cloud:
    def __init__(self, host: str, tenant: str) -> None:
        self.host = host
        self.tenant = tenant
        self.auth = ""
        self.user_id = ""

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict:
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
        ts = str(int(time.time()))
        query = urllib.parse.urlencode(params or {})
        data = query if method == "GET" else _sign_data(body or {})
        headers = {
            "User-Agent": f"Android_{self.tenant}",
            "tenantId": self.tenant,
            "ts": ts,
            "nonce": nonce,
            "sign": hashlib.md5((self.auth + ts + nonce + data).encode()).hexdigest(),
        }
        if self.auth:
            headers["authorization"] = self.auth
        if self.user_id:
            headers["id"] = self.user_id
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # host may carry its port, as the domain list writes it ("host:443")
        conn = http.client.HTTPSConnection(self.host, context=ctx, timeout=20)
        try:
            conn.connect()
            der = conn.sock.getpeercert(binary_form=True) or b""
            if hashlib.sha256(der).hexdigest() != PINNED_SHA256:
                raise SystemExit(f"{self.host}: unexpected TLS certificate, stopping")
            conn.request(method, path + (f"?{query}" if query else ""), payload, headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        try:
            return json.loads(raw)
        except ValueError:
            return {"http_status": resp.status, "body": raw[:200].decode(errors="replace")}


def get_domains(region: str, tenant: str, project: str) -> tuple[dict, dict | None]:
    cloud = Cloud(API_HOSTS[region], tenant)
    reply = cloud.request("GET", "/network-service/domains/list",
                          {"tenantId": tenant, "productModeCode": project, "version": "v1"})
    domains = None
    blob = (reply.get("result") or {}).get("domain") if isinstance(reply.get("result"), dict) else None
    if isinstance(blob, str):
        try:
            domains = json.loads(decrypt(tenant, blob))
        except (ValueError, UnicodeDecodeError):
            domains = None
    return reply, domains


def _host(value: str) -> str:
    return value.split("://", 1)[-1].split("/", 1)[0]


def cmd_domains(args) -> int:
    reply, domains = get_domains(args.region, args.tenant, args.project)
    print(f"domains/list on {API_HOSTS[args.region]}: code {reply.get('code')!r}, "
          f"msg {reply.get('msg')!r}")
    if domains is None:
        show("reply", reply)
        print("No domain list: the signing or the encryption is off.")
        return 1
    show("domain list (decrypted with md5(tenantId)[8:24])", domains)
    print("\nThe cloud answers this for any tenantId: the login is what tells a "
          "right one.")
    return 0


def _find(obj, *names):
    """First value under one of `names` (any depth, case-insensitive)."""
    wanted = {_norm(n) for n in names}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _norm(str(key)) in wanted and value not in (None, ""):
                return value
        for value in obj.values():
            found = _find(value, *names)
            if found not in (None, ""):
                return found
    if isinstance(obj, list):
        for value in obj:
            found = _find(value, *names)
            if found not in (None, ""):
                return found
    return None


def cmd_login(args) -> int:
    reply, domains = get_domains(args.region, args.tenant, args.project)
    if domains is None:
        print(f"domains/list: code {reply.get('code')!r}, msg {reply.get('msg')!r}: "
              "try the `domains` command first.")
        return 1
    app_host = _host(str(_find(domains, "APP_api", "app_api") or API_HOSTS[args.region]))
    mqtt_host = args.broker or _find(domains, "MQTT", "mqtt")
    print(f"API {app_host}, MQTT {mqtt_host} (v2 broker: {domains.get('MQTT_v2')})")

    account = args.account.strip()
    if "@" not in account and "-" not in account:
        print("A phone number goes with its country code, as 86-13800000000.")
        return 1
    password = getpass.getpass("Password (not shown): ")
    cloud = Cloud(app_host, args.tenant)
    register_id = "".join(random.choices(string.ascii_letters + string.digits, k=19))
    body = {
        "tenantId": args.tenant,
        "lang": "en",
        "token": None,
        "userId": None,
        "password": encrypt(args.tenant, password),
        "username": encrypt(args.tenant, account),
        "authcode": None,
        "projectType": args.project,
        "versionCode": args.version_code,
        "versionName": args.version_name,
        "phoneBrand": encrypt(args.tenant, "xiaomi_mi 9"),
        "phoneSys": 1,
        "noticeSetting": {"andIpad": register_id, "android": register_id},
    }
    reply = cloud.request("POST", args.login_path, body=body)
    print(f"\nlogin ({args.login_path}): code {reply.get('code')!r}, msg {reply.get('msg')!r}")
    if reply.get("code") != 0:
        show("login reply", reply)
        return 1
    result = reply.get("result") or {}
    cloud.user_id = str(_find(result, "id", "userId", "user_id") or "")
    cloud.auth = str(_find(result, "AUTH", "auth", "token") or "")
    mqtt_token = str(_find(result, "EMQ_TOKEN", "emqToken") or "")
    print(f"user id: {'yes' if cloud.user_id else 'MISSING'}, "
          f"auth token: {'yes' if cloud.auth else 'MISSING'}, "
          f"MQTT token: {'yes' if mqtt_token else 'MISSING'}")
    show("login reply (shape)", reply)

    path = args.devices_path.format(user_id=cloud.user_id)
    reply = cloud.request("GET", path)
    print(f"\ndevices ({args.devices_path}): code {reply.get('code')!r}, "
          f"msg {reply.get('msg')!r}")
    devices = reply.get("result")
    show("devices", devices)
    if not isinstance(devices, list) or not devices:
        return 1
    if args.listen:
        if not (mqtt_host and mqtt_token and cloud.user_id):
            print("No MQTT broker or token: nothing to listen to.")
            return 1
        listen(str(mqtt_host), cloud.user_id, mqtt_token, args.tenant, devices, args.listen)
    return 0


def listen(broker: str, user_id: str, token: str, tenant: str, devices: list,
           seconds: int) -> None:
    import paho.mqtt.client as mqtt

    host, _, port = _host(broker).partition(":")
    port = int(port or 8883)
    robots = [(str(d.get("productId") or d.get("product_id")), str(d.get("sn")))
              for d in devices if isinstance(d, dict) and d.get("sn")]
    serials = {sn: f"<robot {n}>" for n, (_pid, sn) in enumerate(robots, 1)}
    seen: dict[str, object] = {}
    counts: dict[str, int] = {}
    connected = threading.Event()

    def topic_of(topic: str) -> str:
        for sn, label in serials.items():
            topic = topic.replace(sn, label)
        return topic

    def on_connect(client, userdata, flags, rc, properties=None):
        if getattr(rc, "is_failure", rc != 0):
            print(f"\nMQTT refused the connection (code {rc})")
            return
        for pid, sn in robots:
            base = f"/mqtt/{pid}/{sn}/"
            client.subscribe([(base + t, 0) for t in MQTT_TOPICS])
            client.publish(base + "thing/service/property/get", json.dumps({
                "method": "prop.get", "msgId": str(int(time.time() * 1000)),
                "tenantId": tenant, "version": "3.0",
                "params": {"property": KARCHER_PROPERTIES},
            }))
        connected.set()
        print(f"\nMQTT connected to {host}:{port}; listening {seconds} s. Start a clean "
              "in the app, then send the robot home.")

    def on_message(client, userdata, msg):
        counts[topic_of(msg.topic)] = counts.get(topic_of(msg.topic), 0) + 1
        try:
            payload = json.loads(msg.payload.decode("utf-8", "replace"))
        except ValueError:
            payload = f"<{len(msg.payload)} bytes, not JSON>"
        show(f"{time.strftime('%H:%M:%S')} {topic_of(msg.topic)}", payload)
        params = payload.get("params") or payload.get("data") if isinstance(payload, dict) else None
        if isinstance(params, dict):
            for key, value in params.items():
                seen[key] = redact(value, key)

    kwargs = {"client_id": f"{user_id}_{random.getrandbits(64):016x}", "clean_session": True,
              "protocol": mqtt.MQTTv311}
    if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt 2
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
    else:
        client = mqtt.Client(**kwargs)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.username_pw_set(user_id, token)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, 60)
    client.loop_start()
    try:
        if not connected.wait(20):
            print("MQTT: no connection after 20 s")
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
    show("messages per topic", counts or "none")
    show("every property seen (last value)", seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, project=True):
        p.add_argument("--region", choices=sorted(API_HOSTS), default="eu",
                       help="cluster to ask first (default eu)")
        if project:
            p.add_argument("--project", default="android_iot.3i",
                           help="the app's project type / productModeCode")

    p = sub.add_parser("domains", help="reach the cloud, list its hosts (no account)")
    p.add_argument("--tenant", required=True)
    common(p)
    p.set_defaults(func=cmd_domains)

    p = sub.add_parser("login", help="log in, list the robots, optionally listen")
    p.add_argument("--tenant", required=True)
    p.add_argument("--account", required=True, help="e-mail, or phone as 86-138...")
    p.add_argument("--listen", type=int, default=0, metavar="SECONDS",
                   help="then listen to the robots over MQTT for this long")
    p.add_argument("--broker", help="MQTT host:port instead of the domain list's MQTT, "
                   "e.g. its MQTT_v2")
    p.add_argument("--login-path", default=LOGIN_PATH)
    p.add_argument("--devices-path", default=DEVICES_PATH)
    p.add_argument("--version-code", type=int, default=10004)
    p.add_argument("--version-name", default="1.0.4")
    common(p)
    p.set_defaults(func=cmd_login)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
