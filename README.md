# ILIFE Vacuum for Home Assistant

Custom Home Assistant integration for **ILIFE** robot vacuums that use the
**ILIFEHOME** app (Alibaba IoT / 3irobotix cloud). It talks to the cloud API
directly — no Tuya, no MQTT broker to set up — and ships a premium all‑in‑one
Lovelace card.

> Tested with the **ILIFE V3x** on the **ILIFEHOME** app, **EU** region.
> Other 3irobotix‑based ILIFE models may work but are not verified.

## Features

- 🧹 Full vacuum entity: start / pause / stop / return to dock / locate
- 🌀 Suction (Gentle → Max) and 💧 water level, 🧭 cleaning mode (S‑shape / Auto)
- 🟫 Carpet‑recognition switch, 🎮 directional remote buttons, 🗑️ empty bin
- 📅 Per‑day schedules (enable + time)
- 🔋 Battery, brushes and filter wear, last clean, connectivity (online/offline)
- 🗺️ Live map camera + **clickable cleaning history with the day's map**
- 🖼️ Each cleaning is archived as a tiny PNG in `www/ilife_maps/`
- 🧩 Bundled **ILIFE Vacuum Card** (auto‑registered), English + French
- 👥 Multiple vacuums and multiple accounts supported

## Installation (HACS – custom repository)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/maximedeprince/ha-ilife` (type **Integration**).
2. Install **ILIFE Vacuum**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → ILIFE Vacuum**.
4. Enter your ILIFEHOME **email**, **password** and **region**.

The Lovelace card is registered automatically (storage‑mode dashboards). If you
use **YAML‑mode** dashboards, add the resource manually:

```yaml
# configuration.yaml (lovelace resources) or the dashboard's resources:
- url: /ilife_cards/ilife-vacuum-card.js
  type: module
```

## The card

Add a card → **ILIFE Vacuum Card**, pick your vacuum entity — everything else is
detected automatically. It shows the live map, controls, schedules and a
clickable history (tap a cleaning to open its full map).

## Notes

- Credentials are stored by Home Assistant in the config entry; they are never
  written to logs. The app's shared API keys are baked in (identical for all
  ILIFE users) — no personal secret is included in this repository.
- This is an unofficial integration, not affiliated with ILIFE or 3irobotix.

## License

MIT — see [LICENSE](LICENSE).
