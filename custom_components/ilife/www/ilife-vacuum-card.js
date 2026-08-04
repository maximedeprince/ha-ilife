/*
 * ILIFE Vacuum Card — all-in-one Lovelace card for the ILIFE integration.
 * Vanilla JS, theme-aware, no external dependency (CSP-safe).
 * Auto-discovers the vacuum entities by translation_key (language-independent) and
 * decodes the history mini-maps (CleanMapData, 2bpp bitmap) client-side.
 */

const FLOOR_COLOR = "#c47b5e"; // cleaned floor (terracotta, like the app)
const WALL_COLOR = "#43464f";  // walls / obstacles

const T = {
  en: {
    start: "Start", pause: "Pause", resume: "Resume", stop: "Stop", dock: "Dock", locate: "Locate",
    overview: "Overview", settings: "Settings", suction: "Suction", water: "Water", mode: "Mode",
    carpet: "Carpet recognition", manual: "Manual control", schedules: "Schedules", history: "History",
    empty_bin: "Empty bin", live: "Live", offline: "Offline", battery: "battery", last_run: "Last cleaning",
    cycles: "Cycles", area: "Area", total_time: "Total time", brush: "Brush", filter: "Filter",
    side_brush: "Side brush", reset: "Reset", cancel: "Cancel", reset_confirm: "Reset {part} to 100%?",
    m2_cleaned: "m² cleaned", minutes: "minutes", no_history: "No recent cleanings.",
    not_found: "No ILIFE vacuum entity found.", today: "Today", yesterday: "Yesterday",
    active: "active", session: "session", sessions: "sessions", sleeping: "standby",
    days: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    st: { cleaning: "Cleaning", docked: "Docked", returning: "Returning to dock", paused: "Paused",
          idle: "Idle", error: "Error", unavailable: "Unavailable" },
    locale: "en-US",
  },
  fr: {
    start: "Démarrer", pause: "Pause", resume: "Reprendre", stop: "Stop", dock: "Base", locate: "Localiser",
    overview: "Aperçu", settings: "Réglages", suction: "Aspiration", water: "Eau", mode: "Mode",
    carpet: "Reconnaissance des tapis", manual: "Pilotage manuel", schedules: "Programmations", history: "Historique",
    empty_bin: "Vider le bac", live: "En direct", offline: "Hors ligne", battery: "batterie", last_run: "Dernier passage",
    cycles: "Cycles", area: "Surface", total_time: "Temps cumulé", brush: "Brosse", filter: "Filtre",
    side_brush: "Brosse latérale", reset: "Réinitialiser", cancel: "Annuler", reset_confirm: "Réinitialiser {part} à 100% ?",
    m2_cleaned: "m² nettoyés", minutes: "minutes", no_history: "Aucun nettoyage récent.",
    not_found: "Aucune entité aspirateur ILIFE trouvée.", today: "Aujourd'hui", yesterday: "Hier",
    active: "active", session: "session", sessions: "sessions", sleeping: "en veille",
    days: ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"],
    st: { cleaning: "Nettoyage en cours", docked: "À la base", returning: "Retour à la base", paused: "En pause",
          idle: "Inactif", error: "Erreur", unavailable: "Indisponible" },
    locale: "fr-FR",
  },
};

function decodeCleanMap(b64) {
  if (!b64) return null;
  let bin;
  try { bin = atob(b64); } catch (e) { return null; }
  const b = new Uint8Array(bin.length);
  for (let i = 0; i < b.length; i++) b[i] = bin.charCodeAt(i);
  if (b.length < 5 || b[0] !== 0x01) return null;
  const BPR = b[1] || 3;
  const rows = Math.floor((b.length - 2) / BPR);
  const cells = [];
  for (let r = 0; r < rows; r++) {
    for (let by = 0; by < BPR; by++) {
      const val = b[2 + r * BPR + by];
      for (let p = 0; p < 4; p++) {
        const t = (val >> ((3 - p) * 2)) & 0x3;
        if (t === 1 || t === 2) cells.push({ x: by * 4 + p, y: r, t });
      }
    }
  }
  return { w: BPR * 4, h: rows, cells };
}

function drawMapCells(canvas, cells, maxW, maxH) {
  const ctx = canvas.getContext("2d");
  if (!cells || !cells.length) { canvas.width = 1; canvas.height = 1; ctx.clearRect(0, 0, 1, 1); return; }
  let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity, n1 = 0, n2 = 0;
  for (const c of cells) {
    if (c.x < minx) minx = c.x; if (c.x > maxx) maxx = c.x; if (c.y < miny) miny = c.y; if (c.y > maxy) maxy = c.y;
    if (c.t === 1) n1++; else n2++;
  }
  const floorType = n1 >= n2 ? 1 : 2;
  const cw = maxx - minx + 1, ch = maxy - miny + 1;
  const MG = Math.max(3, Math.round(Math.max(cw, ch) * 0.1));
  const cols = cw + 2 * MG, rows = ch + 2 * MG, cell = 10;
  const W = cols * cell, H = rows * cell;
  canvas.width = W; canvas.height = H;
  const s = Math.min(maxW / W, maxH / H);
  canvas.style.width = Math.max(1, Math.round(W * s)) + "px";
  canvas.style.height = Math.max(1, Math.round(H * s)) + "px";
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, W, H);
  for (const c of cells) {
    ctx.fillStyle = c.t === floorType ? FLOOR_COLOR : WALL_COLOR;
    ctx.fillRect((c.x - minx + MG) * cell, (c.y - miny + MG) * cell, cell, cell);
  }
}

class IlifeVacuumCard extends HTMLElement {
  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this._config = config;
    this._built = false;
    this._padOpen = false;
    this.innerHTML = "";
  }
  set hass(hass) {
    this._hass = hass;
    const lang = (hass.language || hass.locale?.language || "en").toLowerCase();
    this._lang = lang.startsWith("fr") ? "fr" : "en";
    if (!this._built) this._build();
    this._update();
  }
  get _t() { return T[this._lang] || T.en; }
  getCardSize() { return 14; }
  static getStubConfig(hass) {
    const vac = Object.keys(hass.states).find(
      (e) => e.startsWith("vacuum.") && (!hass.entities || hass.entities[e]?.platform === "ilife"));
    return { entity: vac || "" };
  }
  static getConfigElement() { return document.createElement("ilife-vacuum-card-editor"); }

  _resolve() {
    const hass = this._hass, reg = hass.entities || {};
    const cfgEnt = this._config.entity;
    const deviceId = cfgEnt && reg[cfgEnt] ? reg[cfgEnt].device_id : null;
    let ids = deviceId
      ? Object.values(reg).filter((x) => x.device_id === deviceId).map((x) => x.entity_id)
      : Object.values(reg).filter((x) => x.platform === "ilife").map((x) => x.entity_id);
    if (!ids.length && cfgEnt) ids = [cfgEnt];
    const tk = (id) => reg[id]?.translation_key || "";
    const dom = (id) => id.split(".")[0];
    const e = { vacuum: cfgEnt || null, map: null, water: null, mode: null, carpet: null,
      battery: null, history: null, online: null, brush: null, side: null, filter: null, buttons: {}, schedules: {} };
    for (const id of ids) {
      const d = dom(id), k = tk(id);
      if (d === "vacuum") { if (!e.vacuum) e.vacuum = id; }
      else if (d === "camera") { if (k === "map" || !e.map) e.map = id; }
      else if (d === "binary_sensor") { if (k === "online") e.online = id; }
      else if (d === "select") { if (k === "water_level") e.water = id; else if (k === "cleaning_mode") e.mode = id; }
      else if (d === "switch") {
        if (k === "carpet") e.carpet = id;
        else { const m = k.match(/^schedule_(\d+)_enable$/); if (m) (e.schedules[+m[1]] = e.schedules[+m[1]] || {}).enable = id; }
      } else if (d === "time") {
        const m = k.match(/^schedule_(\d+)_time$/); if (m) (e.schedules[+m[1]] = e.schedules[+m[1]] || {}).time = id;
      } else if (d === "button") {
        const map = { forward: "forward", backward: "backward", left: "left", right: "right", rc_pause: "rcstop", dust_collection: "dust",
          reset_main_brush: "resetmain", reset_side_brush: "resetside", reset_filter: "resetfilter" };
        if (map[k]) e.buttons[map[k]] = id;
      } else if (d === "sensor") {
        const dc = hass.states[id]?.attributes.device_class;
        if (dc === "battery") e.battery = id;
        else if (k === "history") e.history = id;
        else if (k === "main_brush") e.brush = id;
        else if (k === "side_brush") e.side = id;
        else if (k === "filter") e.filter = id;
      }
    }
    return e;
  }

  _seg(label, key, opts) {
    if (!opts || !opts.length) return "";
    return `<div class="vc-field"><div class="vc-mlabel">${label}</div>
      <div class="vc-seg" data-seg="${key}" style="--n:${opts.length}">
        <div class="vc-thumb"></div>
        ${opts.map((o) => `<button class="vc-pill" data-val="${o}">${o}</button>`).join("")}
      </div></div>`;
  }

  _build() {
    if (!this._hass) return;
    const hass = this._hass, t = this._t;
    this._ent = this._resolve();
    const e = this._ent;
    if (!e.vacuum) { this.innerHTML = `<ha-card><div style="padding:18px">${t.not_found}</div></ha-card>`; this._built = true; return; }
    const vs = hass.states[e.vacuum] || { attributes: {} };
    const dir = (id, icon) => e.buttons[id] ? `<button class="vc-dir" data-act="${id}"><ha-icon icon="${icon}"></ha-icon></button>` : `<span></span>`;

    let sched = "";
    for (let n = 1; n <= 7; n++) {
      const s = e.schedules[n]; if (!s || (!s.enable && !s.time)) continue;
      sched += `<div class="vc-lrow">
        <span class="vc-sday">${t.days[n - 1]}</span>
        ${s.time ? `<input type="time" class="vc-stime" data-ent="${s.time}">` : "<span></span>"}
        ${s.enable ? `<label class="vc-switch"><input type="checkbox" class="vc-sen" data-ent="${s.enable}"><span class="vc-slider"></span></label>` : ""}
      </div>`;
    }
    const strip = t.days.map((d, i) => `<span class="vc-daypill" data-day="${i + 1}">${d[0]}</span>`).join("");

    this.innerHTML = `
      <ha-card class="ilife-card">
        <style>
          ha-card.ilife-card{margin:0 auto;display:block;}
          .vc{--e1:0 1px 2px rgba(0,0,0,.06),0 1px 3px rgba(0,0,0,.10);
              --hover:color-mix(in srgb, var(--primary-text-color) 6%, var(--secondary-background-color));
              padding:16px;color:var(--primary-text-color);
              font-family:var(--ha-font-family,var(--paper-font-body1_-_font-family),system-ui,sans-serif);}
          @media(min-width:480px){.vc{padding:20px;}}
          .vc *{box-sizing:border-box;}
          /* Responsive: single column on narrow, two columns when the card is wide
             (PC). The .wide class is toggled by a ResizeObserver on the card's own
             width, so it adapts to its placement, not the viewport. */
          /* Masonry-style auto-balancing: each section is an unbreakable block;
             the browser spreads them across 2 (>=560px) or 3 (>=900px) columns to
             even out the heights. Single column on narrow. */
          /* Wide/panel = a "bento" of rounded tiles on the auto-balancing grid. */
          .vc.wide .vc-cols{column-count:var(--cols,2);column-gap:18px;}
          .vc.wide .vc-block{
            background:color-mix(in srgb, var(--primary-text-color) 6%, var(--ha-card-background,var(--card-background-color)));
            border:1px solid color-mix(in srgb, var(--primary-text-color) 12%, transparent);border-radius:22px;padding:16px 18px;margin-top:18px;
            box-shadow:0 2px 10px rgba(0,0,0,.12);
            break-inside:avoid;-webkit-column-break-inside:avoid;}
          .vc.wide .vc-block>:first-child{margin-top:0;}
          .vc.wide .vc-block .vc-sec{margin:0 0 12px;}
          .vc.wide .vc-kpi{background:transparent;border-color:transparent;padding:6px 4px;}
          .vc.wide .vc-map{margin-top:0;}
          /* full_height: fill the whole panel (width AND height) as a bento grid.
             Map = big hero (2x2), the four other tiles fill the right side. */
          .vc.full{display:flex;flex-direction:column;box-sizing:border-box;height:calc(100dvh - var(--header-height, 56px) - 12px);}
          .vc.full .vc-cols{column-count:unset;display:grid;
            grid-template-columns:repeat(4,1fr);grid-template-rows:1fr 1fr;gap:16px;
            flex:1;min-height:0;}
          .vc.full .vc-block{margin-top:0;min-height:0;overflow:auto;}
          .vc.full .blk-map{grid-column:1 / 3;grid-row:1 / 3;overflow:hidden;display:flex;flex-direction:column;}
          .vc.full .blk-map .vc-map{aspect-ratio:auto;flex:1;min-height:0;}
          .vc.full .blk-stats{grid-column:3;grid-row:1;}
          .vc.full .blk-settings{grid-column:3;grid-row:2;}
          .vc.full .blk-sched{grid-column:4;grid-row:1;}
          .vc.full .blk-history{grid-column:4;grid-row:2;}
          /* no schedules -> history fills the whole right column instead of leaving a gap */
          .vc.full .vc-cols:not(:has(.blk-sched)) .blk-history{grid-row:1 / 3;}
          /* full mode content: scale up + distribute so tiles aren't half-empty */
          .vc.full .vc-block{padding:22px 26px;display:flex;flex-direction:column;}
          .vc.full .vc-mlabel{font-size:13px;}
          .vc.full .vc-sec{margin:0 0 18px;}
          .vc.full .vc-sec .r{font-size:13px;}
          .vc.full .blk-stats .vc-kpis{flex:1;grid-template-columns:repeat(2,1fr);grid-auto-rows:1fr;gap:16px;}
          .vc.full .blk-stats .vc-kpi{display:flex;flex-direction:column;justify-content:center;background:color-mix(in srgb,var(--primary-text-color) 4%,transparent);border:1px solid color-mix(in srgb,var(--primary-text-color) 9%,transparent);border-radius:18px;padding:14px 20px;}
          .vc.full .blk-stats .vc-kpi:last-child:nth-child(odd){grid-column:1 / -1;}
          .vc.full .blk-stats .vc-kpi .ic{width:38px;height:38px;margin-bottom:10px;}
          .vc.full .blk-stats .vc-kpi .ic ha-icon{--mdc-icon-size:22px;}
          .vc.full .blk-stats .vc-kpi .v{font-size:30px;}
          .vc.full .blk-stats .vc-kpi .l{font-size:12px;margin-top:6px;}
          .vc.full .blk-settings{justify-content:space-between;}
          .vc.full .blk-settings .vc-field{margin:0;}
          .vc.full .blk-settings .vc-seg{height:46px;}
          .vc.full .blk-settings .vc-pill{font-size:14px;}
          .vc.full .blk-settings .vc-lrow{padding:14px 4px;}
          .vc.full .blk-settings .vc-lrow .lbl{font-size:15px;}
          .vc.full .blk-settings .vc-fold{padding:14px 4px;}
          .vc.full .blk-sched .vc-list{display:flex;flex-direction:column;flex:1;justify-content:space-between;}
          .vc.full .blk-sched .vc-lrow{padding:10px 6px;}
          .vc.full .blk-sched .vc-sday{font-size:16px;}
          .vc.full .blk-sched .vc-stime{font-size:15px;padding:8px 12px;}
          .vc.full .blk-sched .vc-daypill{width:30px;height:30px;font-size:13px;}
          .vc.full .blk-history .vc-hist{max-height:none;flex:1;}
          .vc.full .blk-history .vc-hrow{padding:12px 8px;}
          .vc.full .blk-history .vc-hwhen{font-size:15px;}
          .vc.full .blk-history .vc-hsub{font-size:13px;}
          .vc.full .blk-history .vc-harea{font-size:14px;}
          .vc.full .blk-history .vc-hthumb{width:42px;height:42px;}
          /* full mode: let the hero map fill its big tile (lift the 70%/78% + 360x260 caps) */
          .vc.full .blk-map canvas.vc-mapimg{max-width:none;max-height:none;}
          .vc.full .blk-map img.vc-mapimg{width:auto;height:96%;max-width:96%;}
          .vc [hidden]{display:none!important;}
          .vc-num{font-variant-numeric:tabular-nums;font-feature-settings:'tnum';}
          .vc-mlabel{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--secondary-text-color);}
          .vc-sec{display:flex;align-items:center;justify-content:space-between;margin:24px 0 10px;}
          .vc-sec .r{font-size:12px;color:var(--secondary-text-color);}
          .vc-head{display:flex;align-items:center;gap:12px;min-height:52px;}
          .vc-ava{position:relative;width:44px;height:44px;flex:none;border-radius:50%;display:flex;align-items:center;justify-content:center;
            background:color-mix(in srgb,var(--primary-color) 12%,transparent);color:var(--primary-color);box-shadow:inset 0 0 0 1px var(--divider-color);}
          .vc-ava ha-icon{--mdc-icon-size:24px;}
          .vc-dot{position:absolute;right:-1px;bottom:-1px;width:12px;height:12px;border-radius:50%;background:var(--disabled-text-color);
            box-shadow:0 0 0 2px var(--ha-card-background,var(--card-background-color));}
          .vc.online .vc-dot{background:var(--success-color,#43a047);}
          .vc.cleaning .vc-dot{animation:vcpulse 2s infinite;}
          @keyframes vcpulse{0%,100%{box-shadow:0 0 0 2px var(--card-background-color),0 0 0 4px color-mix(in srgb,var(--success-color,#43a047) 45%,transparent);}50%{box-shadow:0 0 0 2px var(--card-background-color),0 0 0 8px transparent;}}
          .vc.offline .vc-ava{opacity:.55;}
          .vc-id{flex:1;min-width:0;}
          .vc-name{font-size:17px;font-weight:600;letter-spacing:-.01em;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
          .vc-state{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--secondary-text-color);line-height:1.2;margin-top:1px;}
          .vc.cleaning .vc-state{color:var(--primary-color);}
          .vc-batt{display:flex;align-items:center;gap:6px;height:28px;padding:4px 10px;border-radius:999px;background:var(--secondary-background-color);
            font-size:13px;font-weight:600;position:relative;overflow:hidden;flex:none;}
          .vc-batt ha-icon{--mdc-icon-size:17px;}
          .vc-battbar{position:absolute;left:0;bottom:0;height:3px;background:currentColor;border-radius:2px;transition:width .3s;}
          .vc-map{position:relative;margin-top:14px;border-radius:18px;background:var(--secondary-background-color);
            aspect-ratio:4/3;overflow:hidden;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--divider-color) 70%,transparent);
            display:flex;align-items:center;justify-content:center;}
          .vc-map .grid{position:absolute;inset:0;pointer-events:none;opacity:.5;
            background-image:repeating-linear-gradient(0deg,transparent 0 15px,color-mix(in srgb,var(--divider-color) 30%,transparent) 15px 16px),
                             repeating-linear-gradient(90deg,transparent 0 15px,color-mix(in srgb,var(--divider-color) 30%,transparent) 15px 16px);}
          .vc-map .glow{position:absolute;inset:0;pointer-events:none;background:radial-gradient(120% 120% at 50% 35%,color-mix(in srgb,var(--primary-color) 9%,transparent),transparent 60%);}
          .vc-map .vig{position:absolute;inset:0;pointer-events:none;box-shadow:inset 0 -30px 40px -30px rgba(0,0,0,.25);}
          .vc-mapimg{position:relative;max-width:70%;max-height:78%;object-fit:contain;z-index:1;filter:drop-shadow(0 4px 10px rgba(0,0,0,.25));}
          .vc.offline .vc-mapimg{filter:saturate(.5) opacity(.7);}
          .vc-badge{position:absolute;top:10px;left:10px;z-index:3;display:flex;align-items:center;gap:5px;font-size:11px;font-weight:600;
            padding:4px 10px;border-radius:999px;background:color-mix(in srgb,var(--card-background-color) 72%,transparent);
            color:var(--secondary-text-color);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--divider-color) 60%,transparent);backdrop-filter:blur(6px);}
          .vc-badge .bd{width:6px;height:6px;border-radius:50%;background:var(--error-color,#e74c3c);}
          .vc.cleaning .vc-badge .bd{animation:vcpulse2 1.6s infinite;}
          @keyframes vcpulse2{0%,100%{opacity:1;}50%{opacity:.35;}}
          .vc.offline .vc-badge .bd{background:var(--disabled-text-color);animation:none;}
          .vc-scrim{position:absolute;left:0;right:0;bottom:0;z-index:2;padding:22px 14px 10px;display:flex;gap:10px;align-items:baseline;
            background:linear-gradient(transparent,color-mix(in srgb,var(--card-background-color) 85%,transparent));font-size:12px;color:var(--secondary-text-color);}
          .vc-scrim b{font-size:14px;font-weight:700;color:var(--primary-text-color);}
          .vc-modal{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;}
          .vc-modal-bg{position:absolute;inset:0;background:rgba(0,0,0,.62);backdrop-filter:blur(3px);}
          .vc-modal-panel{position:relative;z-index:1;background:var(--card-background-color);border:1px solid var(--divider-color);
            border-radius:20px;padding:16px;width:auto;max-width:min(94vw,640px);max-height:90vh;overflow:auto;
            box-shadow:0 16px 48px rgba(0,0,0,.5);display:flex;flex-direction:column;gap:14px;}
          .vc-modal-head{display:flex;align-items:center;justify-content:space-between;gap:12px;}
          .vc-modal-head .t{font-size:15px;font-weight:600;}
          .vc-modal-head button{width:32px;height:32px;flex:none;border:none;border-radius:50%;cursor:pointer;background:var(--secondary-background-color);color:var(--primary-text-color);font-size:15px;}
          .vc-modal-map{background:var(--secondary-background-color);border-radius:14px;padding:16px;display:flex;align-items:center;justify-content:center;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--divider-color) 70%,transparent);}
          .vc-modal-map canvas{image-rendering:pixelated;max-width:100%;max-height:66vh;width:auto;height:auto;border-radius:6px;filter:drop-shadow(0 4px 10px rgba(0,0,0,.25));}
          .vc-modal-stats{display:flex;justify-content:center;gap:36px;padding:2px 0 4px;}
          .vc-modal-stats .s{text-align:center;}
          .vc-modal-stats .v{font-size:22px;font-weight:700;color:var(--primary-color);}
          .vc-modal-stats .l{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--secondary-text-color);margin-top:2px;}
          .vc-cta{width:100%;height:48px;margin-top:14px;border:none;border-radius:14px;cursor:pointer;
            background:var(--primary-color);color:var(--text-primary-color,#fff);font-size:15px;font-weight:600;
            display:flex;align-items:center;justify-content:center;gap:8px;box-shadow:0 4px 14px color-mix(in srgb,var(--primary-color) 30%,transparent);transition:.15s;}
          .vc-cta:hover{background:color-mix(in srgb,var(--primary-color) 88%,white);}
          .vc-cta:active{transform:scale(.985);}
          .vc-cta ha-icon{--mdc-icon-size:20px;}
          .vc-secs{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px;}
          .vc-sbtn{height:54px;border:1px solid var(--divider-color);border-radius:12px;background:transparent;cursor:pointer;color:var(--secondary-text-color);
            display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;font-size:11px;font-weight:600;transition:.15s;}
          .vc-sbtn:hover{background:var(--hover);color:var(--primary-text-color);transform:translateY(-1px);}
          .vc-sbtn ha-icon{--mdc-icon-size:22px;}
          .vc-sbtn.stop:hover{color:var(--error-color,#db4437);}
          /* When "offline" the vacuum is usually just Wi-Fi asleep at the dock —
             Start/Stop/Dock/Locate must stay clickable (they wake it). Only the
             live directional pad is disabled. */
          .vc-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px;}
          .vc-kpi{position:relative;background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:14px;padding:12px;}
          .vc-kpi-reset{position:absolute;top:8px;right:8px;z-index:2;width:26px;height:26px;border:none;border-radius:999px;cursor:pointer;
            background:color-mix(in srgb,var(--primary-text-color) 8%,transparent);color:var(--secondary-text-color);
            display:flex;align-items:center;justify-content:center;opacity:.55;transition:.15s;}
          .vc-kpi-reset:hover{opacity:1;background:color-mix(in srgb,var(--primary-color) 16%,transparent);color:var(--primary-color);}
          .vc-kpi-reset ha-icon{--mdc-icon-size:16px;}
          .vc-confirm{max-width:min(92vw,380px);text-align:center;}
          .vc-confirm-msg{font-size:15px;font-weight:500;line-height:1.4;padding:6px 4px 4px;}
          .vc-confirm-btns{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px;}
          .vc-cbtn{height:44px;border:none;border-radius:12px;cursor:pointer;font-size:14px;font-weight:600;transition:.15s;}
          .vc-cbtn.cancel{background:var(--secondary-background-color);color:var(--primary-text-color);}
          .vc-cbtn.ok{background:var(--primary-color);color:var(--text-primary-color,#fff);}
          .vc-cbtn:active{transform:scale(.97);}
          .vc-kpi .ic{width:28px;height:28px;border-radius:999px;display:flex;align-items:center;justify-content:center;background:color-mix(in srgb,var(--primary-color) 12%,transparent);color:var(--primary-color);margin-bottom:8px;}
          .vc-kpi .ic ha-icon{--mdc-icon-size:17px;}
          .vc-kpi .v{font-size:20px;font-weight:700;line-height:1;}
          .vc-kpi .l{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--secondary-text-color);margin-top:4px;}
          .vc-gauge{height:4px;border-radius:3px;background:var(--divider-color);margin-top:8px;overflow:hidden;}
          .vc-gauge>i{display:block;height:100%;background:var(--primary-color);border-radius:3px;}
          .vc-field{margin:12px 0;}
          .vc-field .vc-mlabel{margin-bottom:6px;}
          .vc-seg{position:relative;display:grid;grid-template-columns:repeat(var(--n),1fr);background:var(--secondary-background-color);border-radius:999px;padding:3px;height:38px;}
          .vc-thumb{position:absolute;top:3px;bottom:3px;left:3px;width:calc((100% - 6px)/var(--n));border-radius:999px;background:var(--card-background-color);box-shadow:var(--e1);transition:transform .2s cubic-bezier(.2,.8,.2,1);}
          .vc-thumb.hidden{opacity:0;}
          .vc-pill{position:relative;z-index:1;border:none;background:transparent;cursor:pointer;font-size:13px;font-weight:500;color:var(--secondary-text-color);transition:color .15s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
          .vc-pill.active{color:var(--primary-text-color);font-weight:600;}
          .vc-list{border-radius:14px;overflow:hidden;}
          .vc-lrow{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:12px;padding:12px 4px;}
          .vc-lrow + .vc-lrow{border-top:1px solid color-mix(in srgb,var(--divider-color) 60%,transparent);}
          .vc-lrow .lbl{display:flex;align-items:center;gap:10px;font-size:14px;}
          .vc-lrow .lbl ha-icon{--mdc-icon-size:20px;color:var(--secondary-text-color);}
          .vc-sday{font-size:14px;}
          .vc-stime{padding:6px 8px;border-radius:10px;background:var(--card-background-color);color:var(--primary-text-color);border:1px solid var(--divider-color);font-size:14px;font-weight:600;}
          .vc-switch{position:relative;display:inline-block;width:44px;height:24px;flex:none;}
          .vc-switch input{display:none;}
          .vc-slider{position:absolute;inset:0;background:var(--divider-color);border-radius:24px;transition:.2s;cursor:pointer;}
          .vc-slider:before{content:"";position:absolute;height:18px;width:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;}
          .vc-switch input:checked + .vc-slider{background:var(--primary-color);}
          .vc-switch input:checked + .vc-slider:before{transform:translateX(20px);}
          .vc-fold{display:flex;align-items:center;justify-content:space-between;cursor:pointer;padding:12px 4px;user-select:none;}
          .vc-fold .chev{transition:transform .2s;color:var(--secondary-text-color);}
          .vc.padopen .vc-fold .chev{transform:rotate(180deg);}
          .vc-pad{display:grid;grid-template-columns:repeat(3,48px);gap:8px;justify-content:center;padding:4px 0 6px;}
          .vc-dir{width:48px;height:48px;border:1px solid var(--divider-color);border-radius:50%;background:transparent;cursor:pointer;color:var(--secondary-text-color);display:flex;align-items:center;justify-content:center;transition:.15s;}
          .vc-dir:hover{background:var(--hover);color:var(--primary-text-color);}
          .vc-dir:active{transform:scale(.9);}
          .vc-dir ha-icon{--mdc-icon-size:22px;}
          .vc-dust{width:100%;height:44px;margin-top:2px;border:1px solid var(--divider-color);border-radius:12px;background:transparent;cursor:pointer;color:var(--secondary-text-color);display:flex;align-items:center;justify-content:center;gap:8px;font-size:13px;font-weight:600;}
          .vc-dust:hover{background:var(--hover);color:var(--primary-text-color);}
          .vc-strip{display:flex;gap:6px;margin-bottom:8px;}
          .vc-daypill{width:26px;height:26px;border-radius:999px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;background:transparent;color:var(--secondary-text-color);}
          .vc-daypill.on{background:color-mix(in srgb,var(--primary-color) 16%,transparent);color:var(--primary-color);}
          .vc-hist{max-height:230px;overflow-y:auto;-webkit-mask-image:linear-gradient(to bottom,#000 88%,transparent);}
          .vc-hrow{display:grid;grid-template-columns:auto 1fr auto auto;align-items:center;gap:12px;padding:8px 6px;border-radius:12px;cursor:pointer;transition:.12s;}
          .vc-hrow:hover{background:var(--hover);}
          .vc-hthumb{width:34px;height:34px;border-radius:8px;background:var(--secondary-background-color);display:flex;align-items:center;justify-content:center;overflow:hidden;box-shadow:inset 0 0 0 1px var(--divider-color);}
          .vc-hthumb canvas{image-rendering:pixelated;max-width:100%;max-height:100%;}
          .vc-hmeta{min-width:0;}
          .vc-hwhen{font-size:14px;font-weight:600;line-height:1.2;}
          .vc-hsub{font-size:12px;color:var(--secondary-text-color);}
          .vc-harea{font-size:13px;font-weight:600;}
          .vc-hchev{color:var(--secondary-text-color);--mdc-icon-size:18px;}
          .vc-empty{padding:14px;color:var(--secondary-text-color);font-size:13px;text-align:center;}
          @media(prefers-reduced-motion:reduce){.vc *{animation:none!important;transition:none!important;}}
        </style>
        <div class="vc" data-el="root">
          <div class="vc-head">
            <div class="vc-ava"><ha-icon icon="mdi:robot-vacuum"></ha-icon><span class="vc-dot"></span></div>
            <div class="vc-id"><div class="vc-name" data-el="name"></div><div class="vc-state"><span data-el="state"></span></div></div>
            <div class="vc-batt vc-num" data-el="batt"><ha-icon data-el="batticon" icon="mdi:battery"></ha-icon><span data-el="battval"></span><i class="vc-battbar" data-el="battbar"></i></div>
          </div>
          <div class="vc-cols">
          <div class="vc-block blk-map">
          ${e.map ? `<div class="vc-map">
            <div class="glow"></div><div class="grid"></div>
            <img class="vc-mapimg" data-el="map" alt="map">
            <canvas class="vc-mapimg" data-el="heromap" hidden></canvas>
            <div class="vig"></div>
            <span class="vc-badge"><span class="bd"></span><span data-el="badgetxt">${t.live}</span></span>
            <div class="vc-scrim vc-num" data-el="scrim" hidden></div>
          </div>` : ""}
          <button class="vc-cta" data-el="cta"><ha-icon data-el="ctaicon" icon="mdi:play"></ha-icon><span data-el="ctatxt">${t.start}</span></button>
          <div class="vc-secs">
            <button class="vc-sbtn stop" data-act="stop"><ha-icon icon="mdi:stop"></ha-icon><span>${t.stop}</span></button>
            <button class="vc-sbtn" data-act="dock"><ha-icon icon="mdi:home-import-outline"></ha-icon><span>${t.dock}</span></button>
            <button class="vc-sbtn" data-act="locate"><ha-icon icon="mdi:map-marker"></ha-icon><span>${t.locate}</span></button>
          </div>
          </div>
          <div class="vc-block blk-stats">
          <div class="vc-sec"><span class="vc-mlabel">${t.overview}</span></div>
          <div class="vc-kpis" data-el="kpis"></div>
          </div>
          <div class="vc-block blk-settings">
          <div class="vc-sec"><span class="vc-mlabel">${t.settings}</span></div>
          ${this._seg(t.suction, "fan", (vs.attributes.fan_speed_list) || [])}
          ${this._seg(t.water, "water", (e.water && hass.states[e.water]?.attributes.options) || [])}
          ${this._seg(t.mode, "mode", (e.mode && hass.states[e.mode]?.attributes.options) || [])}
          ${e.carpet ? `<div class="vc-list"><div class="vc-lrow"><span class="lbl"><ha-icon icon="mdi:rug"></ha-icon>${t.carpet}</span><span></span>
            <label class="vc-switch"><input type="checkbox" data-el="carpet"><span class="vc-slider"></span></label></div></div>` : ""}
          ${Object.keys(e.buttons).length ? `
          <div class="vc-fold" data-el="fold"><span class="vc-mlabel">${t.manual}</span><ha-icon class="chev" icon="mdi:chevron-down"></ha-icon></div>
          <div data-el="padbox" hidden>
            <div class="vc-pad">
              <span></span>${dir("forward", "mdi:arrow-up-bold")}<span></span>
              ${dir("left", "mdi:arrow-left-bold")}${dir("rcstop", "mdi:pause")}${dir("right", "mdi:arrow-right-bold")}
              <span></span>${dir("backward", "mdi:arrow-down-bold")}<span></span>
            </div>
            ${e.buttons.dust ? `<button class="vc-dust" data-act="dust"><ha-icon icon="mdi:delete-empty"></ha-icon>${t.empty_bin}</button>` : ""}
          </div>` : ""}
          </div>
          ${sched ? `<div class="vc-block blk-sched"><div class="vc-sec"><span class="vc-mlabel">${t.schedules}</span><span class="r" data-el="schedcount"></span></div>
            <div class="vc-strip">${strip}</div><div class="vc-list">${sched}</div></div>` : ""}
          <div class="vc-block blk-history">
          <div class="vc-sec"><span class="vc-mlabel">${t.history}</span><span class="r" data-el="histcount"></span></div>
          <div class="vc-hist" data-el="hist"></div>
          </div>
          </div>
          <div class="vc-modal" data-el="modal" hidden>
            <div class="vc-modal-bg" data-el="modalbg"></div>
            <div class="vc-modal-panel">
              <div class="vc-modal-head"><span class="t" data-el="mcap"></span><button data-el="mclose">✕</button></div>
              <div class="vc-modal-map"><canvas data-el="mcanvas"></canvas></div>
              <div class="vc-modal-stats vc-num" data-el="mstats"></div>
            </div>
          </div>
          <div class="vc-modal" data-el="confirm" hidden>
            <div class="vc-modal-bg" data-el="confirmbg"></div>
            <div class="vc-modal-panel vc-confirm">
              <div class="vc-confirm-msg" data-el="confirmmsg"></div>
              <div class="vc-confirm-btns">
                <button class="vc-cbtn cancel" data-el="confirmno">${t.cancel}</button>
                <button class="vc-cbtn ok" data-el="confirmyes">${t.reset}</button>
              </div>
            </div>
          </div>
        </div>`;

    this._wire();
    this._built = true;
    this._startMapTimer();
    this._observeWidth();
  }

  _observeWidth() {
    const root = this.querySelector('[data-el="root"]');
    if (!root || typeof ResizeObserver === "undefined") return;
    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver((ents) => {
      const w = ents[0].contentRect.width;
      // One column per ~360px, capped at the number of sections (6): the card
      // fills a wide/panel placement, yet stays narrow inside a grid column.
      const cols = Math.max(1, Math.min(6, Math.round(w / 360)));
      root.style.setProperty("--cols", cols);
      root.classList.toggle("wide", cols >= 2);
      // full-page bento: auto when the card takes ~all the width (single-card /
      // panel view); off in a normal grid column. `full_height` config overrides.
      const near = w / (window.innerWidth || w) >= 0.78;
      const cfg = this._config || {};
      const wantFull = cfg.full_height !== undefined ? !!cfg.full_height : near;
      root.classList.toggle("full", wantFull && w >= 900);
      this._scheduleFit();
    });
    this._ro.observe(root);
    // Refit the hero map whenever its tile changes size (full-page mode grows it).
    if (this._roMap) this._roMap.disconnect();
    this._roMap = null;
    const mbox = this.querySelector(".blk-map .vc-map");
    if (mbox && typeof ResizeObserver !== "undefined") {
      this._roMap = new ResizeObserver(() => this._scheduleFit());
      this._roMap.observe(mbox);
    }
  }

  // Coalesce refit calls from both observers into at most one draw per frame.
  _scheduleFit() {
    if (this._fitRaf) return;
    const raf = (typeof requestAnimationFrame === "function") ? requestAnimationFrame : (cb) => cb();
    this._fitRaf = raf(() => { this._fitRaf = 0; this._fitHero(); });
  }

  _fitHero() {
    const hero = this.querySelector('[data-el="heromap"]');
    if (!hero || hero.hidden || !this._heroCells || !this._heroCells.length) return;
    const box = hero.parentElement; if (!box) return;               // .vc-map
    const full = this.querySelector('[data-el="root"]')?.classList.contains("full");
    let mw = 360, mh = 260, key = "nf";
    if (full) {
      const r = box.getBoundingClientRect();
      if (r.width < 40 || r.height < 40) return;                     // not laid out yet
      mw = r.width - 28; mh = r.height - 28;
      key = "f:" + Math.round(r.width) + "x" + Math.round(r.height);
    }
    // Skip redundant redraws (esp. resize-driven) when nothing relevant changed.
    if (key === this._fitKey && this._fitCells === this._heroCells) return;
    this._fitKey = key; this._fitCells = this._heroCells;
    drawMapCells(hero, this._heroCells, mw, mh);
  }

  _wire() {
    const hass = this._hass, e = this._ent;
    const call = (dom, srv, data) => hass.callService(dom, srv, data);
    const q = (s) => this.querySelector(`[data-el="${s}"]`);
    q("cta")?.addEventListener("click", () => {
      const st = hass.states[e.vacuum]?.state;
      if (st === "cleaning") call("vacuum", "pause", { entity_id: e.vacuum });
      else call("vacuum", "start", { entity_id: e.vacuum });
    });
    this.querySelectorAll(".vc-sbtn").forEach((b) => b.addEventListener("click", () => {
      const a = b.dataset.act;
      if (a === "stop") call("vacuum", "stop", { entity_id: e.vacuum });
      else if (a === "dock") call("vacuum", "return_to_base", { entity_id: e.vacuum });
      else if (a === "locate") call("vacuum", "locate", { entity_id: e.vacuum });
    }));
    this.querySelectorAll(".vc-dir,.vc-dust").forEach((b) => b.addEventListener("click", () => {
      const a = b.dataset.act; if (e.buttons[a]) call("button", "press", { entity_id: e.buttons[a] });
    }));
    this.querySelectorAll(".vc-seg").forEach((seg) => seg.addEventListener("click", (ev) => {
      const pill = ev.target.closest(".vc-pill"); if (!pill) return;
      const key = seg.dataset.seg, val = pill.dataset.val;
      if (key === "fan") call("vacuum", "set_fan_speed", { entity_id: e.vacuum, fan_speed: val });
      else if (key === "water") call("select", "select_option", { entity_id: e.water, option: val });
      else if (key === "mode") call("select", "select_option", { entity_id: e.mode, option: val });
    }));
    q("carpet")?.addEventListener("change", (ev) => call("switch", ev.target.checked ? "turn_on" : "turn_off", { entity_id: e.carpet }));
    this.querySelectorAll(".vc-sen").forEach((c) => c.addEventListener("change", () => call("switch", c.checked ? "turn_on" : "turn_off", { entity_id: c.dataset.ent })));
    this.querySelectorAll(".vc-stime").forEach((t) => t.addEventListener("change", () => call("time", "set_value", { entity_id: t.dataset.ent, time: t.value + ":00" })));
    q("fold")?.addEventListener("click", () => { this._padOpen = !this._padOpen; q("padbox").hidden = !this._padOpen; q("root").classList.toggle("padopen", this._padOpen); });
    q("mclose")?.addEventListener("click", () => this._showHistView(null));
    q("modalbg")?.addEventListener("click", () => this._showHistView(null));
    q("kpis")?.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".vc-kpi-reset"); if (!btn) return;
      const key = btn.dataset.reset, id = e.buttons[key]; if (!id) return;
      const tt = this._t, labels = { resetmain: tt.brush, resetside: tt.side_brush, resetfilter: tt.filter };
      this._confirm(tt.reset_confirm.replace("{part}", (labels[key] || "").toLowerCase()),
        () => call("button", "press", { entity_id: id }));
    });
  }

  _confirm(msg, onOk) {
    const modal = this.querySelector('[data-el="confirm"]');
    if (!modal) { if (onOk) onOk(); return; }
    this.querySelector('[data-el="confirmmsg"]').textContent = msg;
    modal.hidden = false;
    const yes = this.querySelector('[data-el="confirmyes"]');
    const no = this.querySelector('[data-el="confirmno"]');
    const bg = this.querySelector('[data-el="confirmbg"]');
    const cleanup = () => {
      modal.hidden = true;
      yes.removeEventListener("click", onYes);
      no.removeEventListener("click", onNo);
      bg.removeEventListener("click", onNo);
    };
    const onYes = () => { cleanup(); if (onOk) onOk(); };
    const onNo = () => cleanup();
    yes.addEventListener("click", onYes);
    no.addEventListener("click", onNo);
    bg.addEventListener("click", onNo);
  }

  _startMapTimer() { if (this._mapTimer) clearInterval(this._mapTimer); this._refreshMap(); this._mapTimer = setInterval(() => this._refreshMap(), 20000); }
  _refreshMap() {
    const e = this._ent; if (!e || !e.map) return;
    const img = this.querySelector('[data-el="map"]'); const st = this._hass.states[e.map];
    const pic = st && st.attributes.entity_picture; if (!img || !pic) return;
    img.src = this._hass.hassUrl(pic) + (pic.includes("?") ? "&" : "?") + "t=" + Date.now();
  }

  _showHistView(clean) {
    const modal = this.querySelector('[data-el="modal"]'); if (!modal) return;
    if (!clean) { modal.hidden = true; return; }
    const dec = decodeCleanMap(clean.map);
    if (!dec || !dec.cells.length) { modal.hidden = true; return; }
    const mw = Math.min((window.innerWidth || 600) * 0.8, 600), mh = (window.innerHeight || 700) * 0.6;
    drawMapCells(this.querySelector('[data-el="mcanvas"]'), dec.cells, mw, mh);
    const cap = this.querySelector('[data-el="mcap"]');
    if (cap) cap.textContent = this._fmtWhen(clean.start, true);
    const stats = this.querySelector('[data-el="mstats"]'), t = this._t;
    if (stats) stats.innerHTML =
      `<div class="s"><div class="v">${clean.area ?? "?"}</div><div class="l">${t.m2_cleaned}</div></div>` +
      `<div class="s"><div class="v">${clean.duration ?? "?"}</div><div class="l">${t.minutes}</div></div>`;
    modal.hidden = false;
  }

  _setActive(key, val) {
    const seg = this.querySelector(`.vc-seg[data-seg="${key}"]`); if (!seg) return;
    const pills = [...seg.querySelectorAll(".vc-pill")];
    let idx = -1;
    pills.forEach((p, i) => { const on = p.dataset.val === val; p.classList.toggle("active", on); if (on) idx = i; });
    const thumb = seg.querySelector(".vc-thumb");
    if (thumb) { if (idx < 0) thumb.classList.add("hidden"); else { thumb.classList.remove("hidden"); thumb.style.transform = `translateX(calc(${idx} * 100%))`; } }
  }

  _fmtWhen(sec, withDate) {
    if (!sec) return "—";
    const t = this._t, d = new Date(sec * 1000), now = new Date();
    const yest = new Date(now); yest.setDate(now.getDate() - 1);
    const hm = d.toLocaleTimeString(t.locale, { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return `${t.today} ${hm}`;
    if (d.toDateString() === yest.toDateString()) return `${t.yesterday} ${hm}`;
    return d.toLocaleDateString(t.locale, { weekday: "short", day: "numeric", month: "short" }) + (withDate ? " " + hm : "");
  }

  _kpi(icon, val, label, gauge, resetKey) {
    return `<div class="vc-kpi">${resetKey ? `<button class="vc-kpi-reset" data-reset="${resetKey}" title="${this._t.reset}"><ha-icon icon="mdi:restore"></ha-icon></button>` : ""}<div class="ic"><ha-icon icon="${icon}"></ha-icon></div>
      <div class="v vc-num">${val}</div><div class="l">${label}</div>
      ${gauge != null ? `<div class="vc-gauge"><i style="width:${Math.max(0, Math.min(100, gauge))}%;${gauge <= 15 ? "background:var(--warning-color,#f9a825)" : ""}"></i></div>` : ""}</div>`;
  }

  _update() {
    if (!this._built || !this._ent || !this._ent.vacuum) return;
    const hass = this._hass, e = this._ent, t = this._t;
    const vs = hass.states[e.vacuum]; if (!vs) return;
    const q = (s) => this.querySelector(`[data-el="${s}"]`);
    const root = q("root");
    const num = (id) => { const s = hass.states[id]; const v = s ? Number(s.state) : NaN; return isFinite(v) ? v : null; };

    const online = e.online ? hass.states[e.online]?.state === "on" : true;
    root.classList.toggle("online", online);
    root.classList.toggle("offline", e.online && !online);
    root.classList.toggle("cleaning", vs.state === "cleaning");

    q("name").textContent = vs.attributes.friendly_name || "ILIFE";
    const stTxt = t.st[vs.state] || vs.state;
    q("state").textContent = (!online && e.online)
      ? (["docked", "idle", "paused"].includes(vs.state) ? `${stTxt} · ${t.sleeping}` : t.offline)
      : stTxt;

    const bv = e.battery ? num(e.battery) : null;
    if (q("batt")) {
      if (bv != null) {
        q("battval").textContent = Math.round(bv) + "%";
        const lvl = Math.round(bv / 10) * 10;
        q("batticon").setAttribute("icon", lvl >= 100 ? "mdi:battery" : lvl <= 0 ? "mdi:battery-outline" : `mdi:battery-${lvl}`);
        q("batt").style.color = bv <= 15 ? "var(--error-color,#db4437)" : bv <= 30 ? "var(--warning-color,#f9a825)" : "var(--secondary-text-color)";
        q("battbar").style.width = Math.max(2, Math.min(100, bv)) + "%";
        q("batt").hidden = false;
      } else q("batt").hidden = true;
    }

    if (q("badgetxt")) q("badgetxt").textContent = (!online && e.online) ? t.sleeping : t.live;

    if (q("scrim")) {
      if (vs.state === "cleaning") {
        const fs = vs.attributes.fan_speed || "";
        q("scrim").innerHTML = `<span><b>${bv != null ? Math.round(bv) : "?"}%</b> ${t.battery}</span>${fs ? `<span>· ${fs}</span>` : ""}`;
        q("scrim").hidden = false;
      } else q("scrim").hidden = true;
    }

    if (q("cta")) {
      const st = vs.state;
      let txt = t.start, ic = "mdi:play";
      if (st === "cleaning") { txt = t.pause; ic = "mdi:pause"; }
      else if (st === "paused") { txt = t.resume; ic = "mdi:play"; }
      q("ctatxt").textContent = txt; q("ctaicon").setAttribute("icon", ic);
    }

    this._setActive("fan", vs.attributes.fan_speed);
    if (e.water) this._setActive("water", hass.states[e.water]?.state);
    if (e.mode) this._setActive("mode", hass.states[e.mode]?.state);

    if (e.carpet && q("carpet")) { const cs = hass.states[e.carpet]; if (document.activeElement !== q("carpet")) q("carpet").checked = cs && cs.state === "on"; }

    const cleans = (e.history && hass.states[e.history]?.attributes.cleans) || [];
    const cycles = cleans.length;

    // Hero map: live camera while cleaning, otherwise the last completed clean's full map
    const img = q("map"), hero = q("heromap");
    if (img && hero) {
      let usedHero = false;
      if (vs.state !== "cleaning") {
        const last = cleans.find((c) => c.map);
        const dec = last && decodeCleanMap(last.map);
        if (dec && dec.cells.length) {
          this._heroCells = dec.cells;
          hero.hidden = false; img.hidden = true; usedHero = true;
          this._fitHero();
          if (q("badgetxt") && online) q("badgetxt").textContent = t.last_run;
        }
      }
      if (!usedHero) { this._heroCells = null; hero.hidden = true; img.hidden = false; }
    }
    const totArea = cleans.reduce((s, c) => s + (Number(c.area) || 0), 0);
    const totMin = cleans.reduce((s, c) => s + (Number(c.duration) || 0), 0);
    const fmtDur = (m) => m >= 60 ? Math.floor(m / 60) + "h" + (m % 60 ? String(m % 60).padStart(2, "0") : "") : m + "min";
    const brush = e.brush ? num(e.brush) : null, side = e.side ? num(e.side) : null, filt = e.filter ? num(e.filter) : null;
    if (q("kpis")) {
      let k = this._kpi("mdi:counter", cycles, t.cycles) +
        this._kpi("mdi:ruler-square", totArea.toFixed(1) + " m²", t.area) +
        this._kpi("mdi:timer-outline", fmtDur(totMin), t.total_time);
      if (brush != null) k += this._kpi("mdi:brush", Math.round(brush) + "%", t.brush, brush, e.buttons.resetmain ? "resetmain" : null);
      if (side != null) k += this._kpi("mdi:brush-variant", Math.round(side) + "%", t.side_brush, side, e.buttons.resetside ? "resetside" : null);
      if (filt != null) k += this._kpi("mdi:air-filter", Math.round(filt) + "%", t.filter, filt, e.buttons.resetfilter ? "resetfilter" : null);
      q("kpis").innerHTML = k;
    }
    if (q("histcount")) q("histcount").textContent = cycles ? `${cycles} ${cycles > 1 ? t.sessions : t.session}` : "";
    if (q("hist")) {
      const maxA = Math.max(1, ...cleans.map((c) => Number(c.area) || 0));
      q("hist").innerHTML = cleans.length
        ? cleans.slice(0, 12).map((c, i) => `<div class="vc-hrow" data-idx="${i}">
            <span class="vc-hthumb"></span>
            <span class="vc-hmeta"><div class="vc-hwhen">${this._fmtWhen(c.start)}</div>
              <div class="vc-hsub"><span style="display:inline-block;width:${Math.round((Number(c.area) || 0) / maxA * 100)}%;max-width:60px;height:3px;border-radius:2px;background:color-mix(in srgb,var(--primary-color) 60%,transparent);vertical-align:middle"></span></div></span>
            <span class="vc-harea vc-num">${c.area ?? "?"} m² · ${c.duration ?? "?"} min</span>
            <ha-icon class="vc-hchev" icon="mdi:chevron-right"></ha-icon></div>`).join("")
        : `<div class="vc-empty">${t.no_history}</div>`;
      q("hist").querySelectorAll(".vc-hrow").forEach((row) => {
        const c = cleans[Number(row.dataset.idx)];
        const th = row.querySelector(".vc-hthumb");
        const dec = c && decodeCleanMap(c.map);
        if (dec && dec.cells.length) { const cv = document.createElement("canvas"); th.innerHTML = ""; th.appendChild(cv); drawMapCells(cv, dec.cells, 34, 34); }
        else th.innerHTML = `<ha-icon icon="mdi:broom" style="--mdc-icon-size:16px;color:var(--secondary-text-color)"></ha-icon>`;
        row.addEventListener("click", () => { if (c && c.map) this._showHistView(c); });
      });
    }

    const onDays = [];
    for (let n = 1; n <= 7; n++) { const s = e.schedules[n]; if (s && s.enable && hass.states[s.enable]?.state === "on") onDays.push(n); }
    if (q("schedcount")) q("schedcount").textContent = onDays.length + " " + t.active;
    this.querySelectorAll(".vc-daypill").forEach((p) => p.classList.toggle("on", onDays.includes(Number(p.dataset.day))));
    this.querySelectorAll(".vc-sen").forEach((c) => { const s = hass.states[c.dataset.ent]; if (s && document.activeElement !== c) c.checked = s.state === "on"; });
    this.querySelectorAll(".vc-stime").forEach((t2) => { const s = hass.states[t2.dataset.ent]; if (s && s.state && s.state.length >= 5 && document.activeElement !== t2) t2.value = s.state.slice(0, 5); });
  }

  disconnectedCallback() { if (this._mapTimer) clearInterval(this._mapTimer); if (this._ro) this._ro.disconnect(); if (this._roMap) this._roMap.disconnect(); if (this._fitRaf && typeof cancelAnimationFrame === "function") cancelAnimationFrame(this._fitRaf); }
}
if (!customElements.get("ilife-vacuum-card")) customElements.define("ilife-vacuum-card", IlifeVacuumCard);

// Visual editor built on the native <ha-form> selector, so adding the card is a
// modern point-and-click flow (pick the vacuum; everything else is auto-detected).
const EDITOR_SCHEMA = [{ name: "entity", selector: { entity: { domain: "vacuum" } } }];
class IlifeVacuumCardEditor extends HTMLElement {
  setConfig(config) { this._config = Object.assign({}, config); this._render(); }
  set hass(hass) { this._hass = hass; this._render(); }
  _render() {
    if (!this._hass) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => (s.name === "entity" ? "ILIFE vacuum" : s.name);
      this._form.addEventListener("value-changed", (ev) => {
        this._config = ev.detail.value;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = this._config;
  }
}
if (!customElements.get("ilife-vacuum-card-editor")) customElements.define("ilife-vacuum-card-editor", IlifeVacuumCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({ type: "ilife-vacuum-card", name: "ILIFE Vacuum Card",
  description: "All-in-one card for the ILIFE vacuum (map, controls, schedules, clickable history).", preview: true });
console.info("%c ILIFE-VACUUM-CARD %c loaded ", "color:#fff;background:#7cadff;border-radius:3px 0 0 3px;padding:2px", "background:#333;color:#fff;border-radius:0 3px 3px 0;padding:2px");
