"""Whitelabel brand profiles.

ILIFE ships several rebranded apps on the *same* Alibaba Living Link platform. They
differ only in a small tenant profile: the API-Gateway appKey/appSecret used to sign
requests, the OpenAccount appID/appVersion sent at login, and the default cloud region.
Everything else (login handshake, RSA password key, endpoints, device/vacuum logic) is
shared, so adding a brand is just a new entry here + a picker in the config flow.

Secrets note: like the ILIFE app secret, each appSecret below is the *app's* shared
secret (identical for every user of that app), used only to sign API-Gateway calls.
User authentication is the email/password OpenAccount login, never these.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Brand:
    key: str            # internal id / config value
    name: str           # label shown in the config flow
    appkey: str         # API-Gateway x-ca-key
    appsecret: bytes    # API-Gateway HMAC-SHA1 secret
    oa_app_id: str      # OpenAccount riskControlInfo.appID (the app package)
    app_version: str    # OpenAccount appVersion / appVersionName
    default_region: str  # default REGIONS key
    sdk_version: str = "3.4.2"  # OpenAccount SDK version reported at connect


BRANDS: dict[str, Brand] = {
    # Original ILIFE app.
    "ilife": Brand(
        key="ilife", name="ILIFE",
        appkey="29416808", appsecret=b"784477c4f4e56b453da510f248282844",
        oa_app_id="com.ilife.home.global", app_version="1.9.43",
        default_region="eu",
    ),
    # AVA PRO MAX whitelabel (com.robot.ava).
    "ava": Brand(
        key="ava", name="AVA",
        appkey="33417005", appsecret=b"91a6acb3ca23cb3983212ee5c0fe002a",
        oa_app_id="com.robot.ava", app_version="1.0.20",
        default_region="us",
    ),
}

DEFAULT_BRAND = "ilife"


def get_brand(key: str | None) -> Brand:
    return BRANDS.get(key or DEFAULT_BRAND, BRANDS[DEFAULT_BRAND])
