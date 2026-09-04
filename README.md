# ILIFE Vacuum for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/maximedeprince/ha-ilife)](https://github.com/maximedeprince/ha-ilife/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Custom Home Assistant integration for **ILIFE** robot vacuums that use the
**ILIFEHOME** app (Alibaba IoT / 3irobotix cloud). It talks to the cloud API
directly — no Tuya, no MQTT broker to set up — and ships a premium all‑in‑one
Lovelace card.

<p align="center">
  <img src="docs/screenshot-1.png" width="300" alt="Card — status, map and controls">
  &nbsp;&nbsp;
  <img src="docs/screenshot-2.png" width="300" alt="Card — schedules and clickable history">
</p>

> Tested with the **ILIFE V3x** on the **ILIFEHOME** app, **EU** region.
> Other 3irobotix‑based ILIFE models likely work — **testers welcome** (see below).


## Whitelabels (multi-brand)

ILIFE ships several rebranded apps on the **same** Alibaba Living Link platform (same
login handshake, endpoints and device logic) — they differ only in a small tenant
profile. The integration supports these via **brand profiles** (`brands.py`), chosen
in the config flow:

| Brand | App / package | IoT appKey | Default region |
|-------|---------------|-----------|----------------|
| `ilife` | ILIFE (`com.ilife.home.global`) | 29416808 | eu |
| `ava`   | AVA PRO MAX (`com.robot.ava`)   | 33417005 | us |

Adding a brand = one entry in `brands.py` (its API-Gateway appKey/appSecret, OpenAccount
appID/appVersion and default region) + it appears in the setup dropdown automatically.
The AVA profile was validated end-to-end against the live us-east-1 cloud.


## Features

- 🧹 Full vacuum entity: start / pause / stop / return to dock / locate
- 🌀 Suction (Gentle → Max) and 💧 water level, 🧭 cleaning mode (S‑shape / Auto)
- 🟫 Carpet‑recognition switch, 🎮 directional remote buttons, 🗑️ empty bin
- 📅 Per‑day schedules (enable + time)
- 🔋 Battery, brushes and filter wear, last clean, connectivity (online/offline)
- 🗺️ Live map camera + **clickable cleaning history with the day's map**
- 🖼️ Each cleaning is archived as a tiny PNG in `www/ilife_maps/`
- 🧩 Bundled **ILIFE Vacuum Card** — added from the UI, **responsive** (2 columns on desktop, 1 on mobile), English + French
- 👥 Multiple vacuums and multiple accounts supported
- 🧾 **Download diagnostics** (credentials redacted) and 🗑️ **remove old / replaced devices** from the UI

## Installation (HACS)

1. HACS → search **ILIFE Vacuum** → **Download** (or add this repo as a custom repository, type *Integration*).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → ILIFE Vacuum**.
4. Enter your ILIFEHOME **email**, **password** and **region**.

## The card — add it from the UI

Edit a dashboard → **Add card** → search **ILIFE Vacuum Card** → pick your vacuum
in the visual editor. Everything else (map, sensors, schedules…) is detected
automatically. No YAML needed.

The card is registered automatically for storage‑mode dashboards. For
**YAML‑mode** dashboards, add the resource manually:

```yaml
- url: /ilife_cards/ilife-vacuum-card.js
  type: module
```

## 🙏 Help wanted — testers for other ILIFE models

The code is written to be generic, so other 3irobotix‑based ILIFE vacuums have a
good chance of working. If you own a **different model**, please try it and
[open an issue](https://github.com/maximedeprince/ha-ilife/issues/new/choose)
with your model, **debug logs** and the **diagnostics file** (see below) — that's
what lets me add support. For a **blank or wrong map**, the diagnostics file is
the key: it contains the raw map fields your model actually reports.

## Troubleshooting — debug logs

Add this to `configuration.yaml`, restart, reproduce the issue, then copy the
`custom_components.ilife` lines from **Settings → System → Logs**:

```yaml
logger:
  default: info
  logs:
    custom_components.ilife: debug
```

### Diagnostics file

For map or model‑specific problems, the fastest way to help is the diagnostics
file. Go to **Settings → Devices & Services → ILIFE Vacuum → ⋮ → Download
diagnostics** (there is also a per‑device button). It contains the raw property
payload each vacuum reports — including the map data — with your email, password
and device IDs **redacted**. Attach it to the issue.

### Removing an old device

Replaced or sold a vacuum? Once it is gone from your ILIFE account, open the
device page → **⋮ → Delete**. Devices that are still on the account cannot be
removed this way (they would just come back on the next refresh).

## Notes

- Credentials are stored by Home Assistant in the config entry; they are never
  written to logs. The app's shared API keys are baked in (identical for all
  ILIFE users) — no personal secret is included in this repository.
- This is an unofficial integration, not affiliated with ILIFE or 3irobotix.

## License

MIT — see [LICENSE](LICENSE).
