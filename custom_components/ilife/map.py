"""Décodage + rendu de la carte ILIFE (format propriétaire 3irobotix).

RealTimeMap.MapData = suite de records 5 octets : [int16 x BE][int16 y BE][uint8 type].
  type 1 = mur/obstacle, type 2 = sol nettoyé.
CurrentPiont / ChargerPiont = position packée : (x<<16)|(y & 0xFFFF), deux int16.
"""
from __future__ import annotations

import base64
import io
import struct

WALL = 1
FLOOR = 2

# couleurs RGBA
C_FLOOR = (124, 173, 255, 255)   # sol nettoyé (bleu clair)
C_WALL = (60, 63, 74, 255)       # murs
C_ROBOT = (231, 76, 60, 255)     # robot (rouge)
C_CHARGER = (46, 204, 113, 255)  # base (vert)
C_BG = (255, 255, 255, 0)        # fond transparent


def unpack_point(v):
    """Position packée -> (x, y) ou None."""
    if not isinstance(v, int):
        return None
    u = v & 0xFFFFFFFF
    x = struct.unpack(">h", struct.pack(">H", (u >> 16) & 0xFFFF))[0]
    y = struct.unpack(">h", struct.pack(">H", u & 0xFFFF))[0]
    return x, y


def decode_cells(b64):
    """Décode MapData -> liste de (x, y, type)."""
    if not b64:
        return []
    try:
        d = base64.b64decode(b64)
    except Exception:
        return []
    if not d or len(d) % 5:
        return []
    out = []
    for i in range(0, len(d), 5):
        x, y = struct.unpack(">hh", d[i:i + 4])
        out.append((x, y, d[i + 4]))
    return out


def encode_cells(cells):
    """Ré-encode des cellules -> MapData base64 (records 5 octets [int16 x][int16 y][uint8 type]).
    `cells` = dict {(x, y): type} ou itérable de (x, y, type)."""
    out = bytearray()
    items = cells.items() if isinstance(cells, dict) else cells
    for item in items:
        (x, y), t = (item[0], item[1]) if isinstance(cells, dict) else ((item[0], item[1]), item[2])
        try:
            out += struct.pack(">hhB", int(x), int(y), int(t) & 0xFF)
        except struct.error:
            continue
    return base64.b64encode(bytes(out)).decode()


def decode_clean_bitmap(b64):
    """CleanMapData assemblé (bitmap 2 bits/pixel) -> liste (x, y, type) pour type in {1,2}."""
    if not b64:
        return []
    try:
        b = base64.b64decode(b64)
    except Exception:
        return []
    if len(b) < 5 or b[0] != 0x01:
        return []
    bpr = b[1]
    data = b[2:]
    rows = len(data) // bpr if bpr else 0
    out = []
    for r in range(rows):
        for by in range(bpr):
            val = data[r * bpr + by]
            for p in range(4):
                t = (val >> ((3 - p) * 2)) & 0x3
                if t in (1, 2):
                    out.append((by * 4 + p, r, t))
    return out


def render_clean_map_png(b64, cell=8, margin=2):
    """Rend un CleanMapData (plan d'historique assemblé) en PNG (bytes), style app :
    sol terracotta (type majoritaire), murs sombres, fond transparent, marge autour."""
    from PIL import Image, ImageDraw

    cells = decode_clean_bitmap(b64)
    if not cells:
        return None
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    n1 = sum(1 for c in cells if c[2] == 1)
    floor = 1 if n1 >= (len(cells) - n1) else 2
    cols = (maxx - minx + 1) + 2 * margin
    rows = (maxy - miny + 1) + 2 * margin
    FLOOR = (196, 123, 94, 255)
    WALL = (67, 70, 79, 255)
    img = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for x, y, t in cells:
        col = FLOOR if t == floor else WALL
        cx = (x - minx + margin) * cell
        cy = (y - miny + margin) * cell
        draw.rectangle([cx, cy, cx + cell - 1, cy + cell - 1], fill=col)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
#  Carte des modèles récents (L100, ...) : RealMapData_1
# --------------------------------------------------------------------------- #
# Ces modèles ne publient PAS RealTimeMap. Leur carte vit dans
# RealMapData_1.MapData1..8 (à concaténer), sous forme d'un raster 2 bits
# compressé en RLE :
#   en-tête 8 octets : [b0][0x32][originX int16 BE][originY int16 BE][width int16 BE]
#   corps : paires (valeur, count) ; type de cellule = valeur % 10
#           (1 mur, 2 sol nettoyé, 3 vide/hors-carte, 0 inconnu ;
#            les valeurs 11/12/13 = type+10, un drapeau de couche).
def decode_realmap(state):
    """RealMapData_1 -> liste (x, y, type) pour type in {1, 2}, sinon []."""
    obj = (state or {}).get("RealMapData_1") or {}
    raw = b""
    for i in range(1, 9):
        part = obj.get(f"MapData{i}")
        if part:
            try:
                raw += base64.b64decode(part)
            except Exception:
                return []
    if len(raw) < 8:
        return []
    width = struct.unpack(">h", raw[6:8])[0]
    if width <= 0:
        return []
    body = raw[8:]
    out = []
    pos = 0
    i = 0
    end = len(body) - 1
    while i < end:
        t = body[i] % 10
        cnt = body[i + 1]
        if t in (WALL, FLOOR):
            for k in range(cnt):
                p = pos + k
                out.append((p % width, p // width, t))
        pos += cnt
        i += 2
    return out


def render_realmap_png(state):
    """Rend la carte RealMapData_1 (modèles L100...) en PNG (bytes) ou None.
    Raster dense : on recadre sur la zone réelle et on borne la taille de cellule
    (le raster complet fait des centaines de cellules de côté, surtout du vide)."""
    from PIL import Image, ImageDraw

    cells = decode_realmap(state)
    if not cells:
        return None
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    cols = maxx - minx + 1
    rows = maxy - miny + 1
    margin = 2
    cell = max(2, min(10, 900 // max(cols, rows, 1)))
    img = Image.new("RGBA", ((cols + 2 * margin) * cell, (rows + 2 * margin) * cell), C_BG)
    draw = ImageDraw.Draw(img)
    for x, y, t in cells:
        col = C_FLOOR if t == FLOOR else C_WALL
        cx = (x - minx + margin) * cell
        cy = (y - miny + margin) * cell
        draw.rectangle([cx, cy, cx + cell - 1, cy + cell - 1], fill=col)
    # The raw raster comes out landscape; rotate 90° clockwise to match the app's
    # portrait orientation (validated against a user's L100 app screenshot).
    img = img.rotate(270, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_png(state):
    """Rend la carte temps réel en PNG (bytes) ou None si pas de données.
    Supersampling + LANCZOS pour un rendu lisse (pas de gros pixels)."""
    from PIL import Image, ImageDraw

    rtm = (state or {}).get("RealTimeMap") or {}
    cells = decode_cells(rtm.get("MapData"))
    if not cells:
        # Modèles récents (L100...) : carte au format RealMapData_1.
        return render_realmap_png(state)

    robot = unpack_point(rtm.get("CurrentPiont"))
    charger = unpack_point((state or {}).get("ChargerPiont"))

    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    for pt in (robot, charger):
        if pt:
            xs.append(pt[0])
            ys.append(pt[1])
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)

    margin = 2
    cols = (maxx - minx + 1) + 2 * margin
    rows = (maxy - miny + 1) + 2 * margin
    # cellule d'affichage large (carte nette une fois mise à l'échelle),
    # dessin en supersampling x3 puis réduction lissée
    cell = max(12, min(46, 720 // max(cols, rows)))
    ss = 3
    cs = cell * ss
    img = Image.new("RGBA", (cols * cs, rows * cs), C_BG)
    draw = ImageDraw.Draw(img)

    def box(x, y, pad=0):
        c = (x - minx + margin) * cs
        r = (maxy - y + margin) * cs  # flip vertical (y monte)
        return [c - pad, r - pad, c + cs - 1 + pad, r + cs - 1 + pad]

    def center(pt):
        b = box(*pt)
        return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2

    # murs puis sol (léger chevauchement pour supprimer les jointures)
    for x, y, t in cells:
        if t == WALL:
            draw.rectangle(box(x, y, ss), fill=C_WALL)
    for x, y, t in cells:
        if t == FLOOR:
            draw.rectangle(box(x, y, ss), fill=C_FLOOR)

    if charger:
        cx, cy = center(charger)
        rr = cs * 0.5
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=C_CHARGER)
    if robot:
        cx, cy = center(robot)
        rh = cs * 1.2  # halo doux
        draw.ellipse([cx - rh, cy - rh, cx + rh, cy + rh], fill=(231, 76, 60, 55))
        rr = cs * 0.82
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(255, 255, 255, 235))
        rr2 = cs * 0.58
        draw.ellipse([cx - rr2, cy - rr2, cx + rr2, cy + rr2], fill=C_ROBOT)

    img = img.resize((cols * cell, rows * cell), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
