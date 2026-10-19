"""Render copy_save.ico (history arrow + clock hands on a rounded blue tile).

Pure Python: shapes are tested per sub-sample (supersampled anti-aliasing),
each size is rendered natively, and the ICO stores PNG images.
"""
import math, struct, zlib, sys

SIZES = [16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80, 96, 128, 256]
C1, C2 = (0x3A, 0x96, 0xF0), (0x00, 0x52, 0xA8)   # gradient top-left -> bottom-right
WHITE = (255, 255, 255)

CX, CY, R, TH = 0.5, 0.5, 0.28, 0.085            # arc centre, radius, thickness
GAP_START, GAP_END = math.radians(150), math.radians(205)  # opening on the left


def in_rounded_rect(x, y, m=0.03, r=0.22):
    x0, y0, x1, y1 = m, m, 1 - m, 1 - m
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def tri(px, py, a, b, c):
    def s(p, q, r):
        return (p[0] - r[0]) * (q[1] - r[1]) - (q[0] - r[0]) * (p[1] - r[1])
    d1, d2, d3 = s((px, py), a, b), s((px, py), b, c), s((px, py), c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


# Arrowhead at the arc's end (angle GAP_START), pointing counter-clockwise.
_a = GAP_START
_P = (CX + R * math.cos(_a), CY - R * math.sin(_a))
_d = (-math.sin(_a), -math.cos(_a))            # CCW tangent in screen coords
_n = (_d[1], -_d[0])
_AH = 0.15
ARROW = ((_P[0] + _d[0] * _AH, _P[1] + _d[1] * _AH),
         (_P[0] + _n[0] * _AH, _P[1] + _n[1] * _AH),
         (_P[0] - _n[0] * _AH, _P[1] - _n[1] * _AH))
HANDS = [((CX, CY), (CX, CY - 0.15)), ((CX, CY), (CX + 0.11, CY + 0.07))]


def in_glyph(x, y):
    d = math.hypot(x - CX, y - CY)
    if abs(d - R) <= TH / 2:
        ang = math.atan2(CY - y, x - CX) % (2 * math.pi)
        if not (GAP_START <= ang <= GAP_END):
            return True
    if tri(x, y, *ARROW):
        return True
    return any(seg_dist(x, y, *a, *b) <= TH * 0.42 for a, b in HANDS)


def render(size):
    ss = 4 if size <= 64 else 2
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) / size
                    y = (py + (sy + 0.5) / ss) / size
                    if not in_rounded_rect(x, y):
                        continue
                    if in_glyph(x, y):
                        col = WHITE
                    else:
                        t = (x + y) / 2
                        col = tuple(c1 + (c2 - c1) * t for c1, c2 in zip(C1, C2))
                    r += col[0]; g += col[1]; b += col[2]; a += 1
            n = ss * ss
            if a:
                row += bytes([round(r / a), round(g / a), round(b / a), round(255 * a / n)])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main(out_ico, preview_png=None):
    pngs = [render(s) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(SIZES))
    offset = 6 + 16 * len(SIZES)
    entries, data = b"", b""
    for s, png in zip(SIZES, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    open(out_ico, "wb").write(header + entries + data)
    if preview_png:
        open(preview_png, "wb").write(pngs[-1])
    print("wrote", out_ico, len(header + entries + data), "bytes")


if __name__ == "__main__":
    main(*(sys.argv[1:] or ["copy_save.ico"]))
