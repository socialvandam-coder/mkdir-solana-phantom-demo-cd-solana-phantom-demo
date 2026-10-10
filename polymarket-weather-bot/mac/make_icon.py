"""Tekent het Weerbot-pictogram (1024x1024 PNG) met alleen de standaardbibliotheek.

Gebruik:  python3 mac/make_icon.py [uitvoer.png]      (standaard: mac/icon.png)

Een afgerond vierkant met een blauwe lucht, een zon, een wolkje en een
thermometer. Randen worden zacht gemaakt met signed distance functions, dus
er is geen Pillow of andere extra bibliotheek nodig.
"""
from __future__ import annotations

import math
import os
import struct
import sys
import zlib
from array import array

SIZE = 1024

# --------------------------------------------------------------- vormen ---


def sd_circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def sd_round_rect(px, py, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
    qx = abs(px - cx) - hx + r
    qy = abs(py - cy) - hy + r
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - r


def sd_capsule(px, py, ax, ay, bx, by, r):
    pax, pay = px - ax, py - ay
    bax, bay = bx - ax, by - ay
    h = (pax * bax + pay * bay) / (bax * bax + bay * bay)
    h = min(1.0, max(0.0, h))
    return math.hypot(pax - bax * h, pay - bay * h) - r


def mix(c0, c1, t):
    t = min(1.0, max(0.0, t))
    return tuple(a + (b - a) * t for a, b in zip(c0, c1))


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


# --------------------------------------------------------------- canvas ---


class Canvas:
    """RGBA-canvas met voorvermenigvuldigde (premultiplied) kleuren."""

    def __init__(self, size):
        self.size = size
        n = size * size
        self.r = array("d", bytes(8 * n))
        self.g = array("d", bytes(8 * n))
        self.b = array("d", bytes(8 * n))
        self.a = array("d", bytes(8 * n))

    def paint(self, bbox, sdf, color, alpha=1.0, soft=1.0):
        """Teken een vorm over het bestaande beeld ("source over").

        bbox: (x0, y0, x1, y1) gebied waarbinnen de vorm ligt
        sdf: f(px, py) -> afstand tot de rand (negatief = binnen)
        color: (r, g, b) of f(px, py) -> (r, g, b)
        soft: breedte van de zachte rand in pixels (1 = gewone anti-aliasing)
        """
        size = self.size
        x0, y0, x1, y1 = (int(max(0, math.floor(bbox[0]))), int(max(0, math.floor(bbox[1]))),
                          int(min(size, math.ceil(bbox[2]))), int(min(size, math.ceil(bbox[3]))))
        R, G, B, A = self.r, self.g, self.b, self.a
        fixed = None if callable(color) else color
        for y in range(y0, y1):
            py = y + 0.5
            row = y * size
            for x in range(x0, x1):
                px = x + 0.5
                d = sdf(px, py)
                cov = 0.5 - d / soft
                if cov <= 0.0:
                    continue
                if cov > 1.0:
                    cov = 1.0
                k = cov * alpha
                cr, cg, cb = fixed if fixed is not None else color(px, py)
                i = row + x
                inv = 1.0 - k
                R[i] = cr * k + R[i] * inv
                G[i] = cg * k + G[i] * inv
                B[i] = cb * k + B[i] * inv
                A[i] = k + A[i] * inv

    def png_bytes(self):
        size = self.size
        R, G, B, A = self.r, self.g, self.b, self.a
        raw = bytearray()
        for y in range(size):
            raw.append(0)  # filter: none
            row = y * size
            for x in range(size):
                i = row + x
                a = A[i]
                if a <= 0.0:
                    raw += b"\x00\x00\x00\x00"
                    continue
                raw.append(min(255, int(R[i] / a * 255 + 0.5)))
                raw.append(min(255, int(G[i] / a * 255 + 0.5)))
                raw.append(min(255, int(B[i] / a * 255 + 0.5)))
                raw.append(min(255, int(a * 255 + 0.5)))

        def chunk(kind, data):
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 bit, RGBA
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


# --------------------------------------------------------------- tekening ---


def draw(c: Canvas) -> None:
    # Afgerond vierkant zoals macOS-pictogrammen (824 px met marge rondom).
    x0, y0, x1, y1, rad = 100, 100, 924, 924, 185

    # zachte schaduw onder het vierkant
    c.paint((x0 - 40, y0 - 10, x1 + 40, y1 + 60),
            lambda px, py: sd_round_rect(px, py - 14, x0, y0, x1, y1, rad),
            (0.05, 0.15, 0.30), alpha=0.30, soft=28.0)

    # lucht: verloop van diepblauw (boven) naar lichtblauw (onder), met een warme gloed rond de zon
    sun_x, sun_y, sun_r = 372, 380, 118
    sky_top, sky_bottom, glow = hex_rgb("#1E7FE0"), hex_rgb("#8ED2FF"), hex_rgb("#FFF1B8")

    def sky_color(px, py):
        base = mix(sky_top, sky_bottom, (py - y0) / (y1 - y0))
        warmth = max(0.0, 1.0 - math.hypot(px - sun_x, py - sun_y) / 430.0)
        return mix(base, glow, 0.55 * warmth * warmth)

    c.paint((x0, y0, x1, y1), lambda px, py: sd_round_rect(px, py, x0, y0, x1, y1, rad), sky_color)

    # zonnestralen
    ray_color = hex_rgb("#FFC21A")
    for k in range(12):
        ang = math.radians(k * 30 + 15)
        ax, ay = sun_x + math.cos(ang) * 152, sun_y + math.sin(ang) * 152
        bx, by = sun_x + math.cos(ang) * 205, sun_y + math.sin(ang) * 205
        c.paint((min(ax, bx) - 20, min(ay, by) - 20, max(ax, bx) + 20, max(ay, by) + 20),
                lambda px, py, ax=ax, ay=ay, bx=bx, by=by: sd_capsule(px, py, ax, ay, bx, by, 15),
                ray_color)

    # zon met licht verloop
    sun_in, sun_out = hex_rgb("#FFE766"), hex_rgb("#FFB000")
    c.paint((sun_x - sun_r - 2, sun_y - sun_r - 2, sun_x + sun_r + 2, sun_y + sun_r + 2),
            lambda px, py: sd_circle(px, py, sun_x, sun_y, sun_r),
            lambda px, py: mix(sun_in, sun_out,
                               math.hypot(px - sun_x + 35, py - sun_y + 35) / (sun_r * 1.6)))

    # wolkje (unie van cirkels en een afgeronde basis)
    def cloud_sdf(px, py, dy=0.0):
        py -= dy
        return min(sd_circle(px, py, 292, 702, 70),
                   sd_circle(px, py, 388, 650, 100),
                   sd_circle(px, py, 478, 706, 66),
                   sd_round_rect(px, py, 222, 665, 544, 774, 37))

    c.paint((190, 530, 570, 820), lambda px, py: cloud_sdf(px, py, 12),
            (0.05, 0.25, 0.45), alpha=0.18, soft=14.0)
    cloud_top, cloud_bottom = hex_rgb("#FFFFFF"), hex_rgb("#DCEBF8")
    c.paint((205, 545, 555, 780), cloud_sdf,
            lambda px, py: mix(cloud_top, cloud_bottom, (py - 600) / 180))

    # thermometer
    tx, top, bottom, bulb_y = 700, 262, 640, 712

    def glass_sdf(px, py, dy=0.0):
        py -= dy
        return min(sd_capsule(px, py, tx, top, tx, bottom, 54), sd_circle(px, py, tx, bulb_y, 98))

    c.paint((tx - 120, top - 70, tx + 120, bulb_y + 130), lambda px, py: glass_sdf(px, py, 12),
            (0.05, 0.25, 0.45), alpha=0.18, soft=14.0)
    c.paint((tx - 102, top - 58, tx + 102, bulb_y + 102), glass_sdf, (1.0, 1.0, 1.0))
    c.paint((tx - 34, top - 34, tx + 34, bottom + 34),
            lambda px, py: sd_capsule(px, py, tx, top, tx, bottom, 30), hex_rgb("#D3E8F8"))
    merc_light, merc_dark = hex_rgb("#FF6B57"), hex_rgb("#D9271C")
    c.paint((tx - 78, 400, tx + 78, bulb_y + 78),
            lambda px, py: min(sd_capsule(px, py, tx, 430, tx, bulb_y, 30),
                               sd_circle(px, py, tx, bulb_y, 74)),
            lambda px, py: mix(merc_light, merc_dark, (px - (tx - 74)) / 148))
    c.paint((tx - 52, bulb_y - 52, tx - 10, bulb_y - 10),
            lambda px, py: sd_circle(px, py, tx - 30, bulb_y - 30, 17),
            (1.0, 1.0, 1.0), alpha=0.55)

    # streepjes van de schaal
    for ty in (310, 370, 430, 490, 550):
        c.paint((768, ty - 12, 812, ty + 12),
                lambda px, py, ty=ty: sd_capsule(px, py, 778, ty, 802, ty, 7),
                (1.0, 1.0, 1.0), alpha=0.9)


def main(argv: list[str]) -> int:
    out = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
    canvas = Canvas(SIZE)
    draw(canvas)
    data = canvas.png_bytes()
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"Pictogram geschreven: {out} ({len(data) // 1024} KB, {SIZE}x{SIZE})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
