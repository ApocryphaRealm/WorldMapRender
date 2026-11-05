"""The road and path layer of a worldspace, from the landscape painting itself.

Terrain LOD textures lose small paths and break thin roads apart at their resolution. The winning LAND record of each cell says,
at each of its 33x33 points, how much of each landscape texture is painted there. This reads those layers from every active plugin
(last plugin wins, as in the game) and returns one image for the whole map: colour of the road's own texture, and how much
road is painted, 32 texels a cell, so a road runs on unbroken from cell to cell and a footpath stays as thin as it was painted.

    roads.py Tamriel x0 y0 x1 y1 out.png     a look at it, and the list of textures taken as roads
"""
import io
import os
import re
import struct
import sys
import time
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import markers as M  # noqa: E402

# Matched on the landscape texture's editor ID and its diffuse file's name.
ROAD_NAMES = re.compile(r"road|path|trail|cobble|street|pavement|paving|flagstone|walkway", re.I)
NOT_ROAD = re.compile(r"roadside|wayside|offroad|tundra", re.I)
FLAG_COMPRESSED = 0x40000
GRID = 32


def scan(world):
    """Landscape texture table, texture sets, and each cell's winning LAND."""
    target = world.lower()
    ltex, txst, lands = {}, {}, {}
    mods = M.active_mods()
    for name in M.active_plugins():
        path = M.find_plugin(name, mods)
        if not path:
            continue
        d = open(path, "rb").read()
        if d[:4] != b"TES4":
            continue
        size = struct.unpack_from("<I", d, 4)[0]
        masters = [s[:-1].decode("latin1").lower() for t, s in M.subs(d[24:24 + size]) if t == b"MAST"]
        plugin = name.lower()
        full = masters + [plugin]

        def form(fid, full=full, plugin=plugin):
            i = fid >> 24
            return (full[i] if i < len(full) else plugin), fid & 0xFFFFFF

        names, parents = {}, {}

        def walk(p, end, cur, coords):
            while p + 24 <= end:
                if d[p:p + 4] == b"GRUP":
                    gs = struct.unpack_from("<I", d, p + 4)[0]
                    gt = struct.unpack_from("<i", d, p + 12)[0]
                    c = cur
                    if gt == 1:
                        c = form(struct.unpack_from("<I", d, p + 8)[0])
                        if names.get(c) != target:   # a walled city's own land paints over its parent's in that world only
                            p += gs
                            continue
                    if gt in (8, 10):                # persistent and visible-distant children hold no LAND
                        p += gs
                        continue
                    walk(p + 24, p + gs, c, coords)
                    p += gs
                    continue
                rt = d[p:p + 4]
                sz, fl, fid = struct.unpack_from("<III", d, p + 4)
                if rt == b"WRLD":
                    body = d[p + 24:p + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    for t, s in M.subs(body):
                        if t == b"EDID":
                            names[form(fid)] = s[:-1].decode("latin1").lower()
                            break
                elif rt == b"CELL" and cur is not None:
                    body = d[p + 24:p + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    for t, s in M.subs(body):
                        if t == b"XCLC" and len(s) >= 8:
                            coords[0] = struct.unpack_from("<2i", s)
                            break
                elif rt == b"LAND" and cur is not None and coords[0] is not None:
                    lands[coords[0]] = (bytes(d[p + 24:p + 24 + sz]), bool(fl & FLAG_COMPRESSED), full, plugin)
                p += 24 + sz

        p = 24 + size
        while p + 24 <= len(d):
            gs = struct.unpack_from("<I", d, p + 4)[0]
            label = d[p + 8:p + 12]
            if label == b"LTEX" or label == b"TXST":
                q = p + 24
                while q < p + gs:
                    sz, fl, fid = struct.unpack_from("<III", d, q + 4)
                    body = d[q + 24:q + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    r = {}
                    for t, s in M.subs(body):
                        if t == b"EDID":
                            r["ed"] = s[:-1].decode("latin1")
                        elif t == b"TNAM" and len(s) == 4:
                            r["set"] = form(struct.unpack_from("<I", s)[0])
                        elif t == b"TX00":
                            r["tex"] = s[:-1].decode("latin1")
                    (ltex if label == b"LTEX" else txst)[form(fid)] = r
                    q += 24 + sz
            elif label == b"WRLD":
                walk(p + 24, p + gs, None, [None])
            p += gs
    return ltex, txst, lands


def classify(ltex, txst, tex_lookup, extra=None):
    """Landscape texture -> its road colour, for those that are roads or paths."""
    roads = {}
    for k, r in ltex.items():
        set_ = txst.get(r.get("set"), {})
        fname = set_.get("tex", "")
        label = r.get("ed", "") + " " + os.path.basename(fname.replace("\\", "/"))
        if (ROAD_NAMES.search(label) and not NOT_ROAD.search(label)) or (extra and re.search(extra, label, re.I)):
            roads[k] = dict(label=label, colour=tex_lookup(fname) if fname else None)
    return roads


def build(world, rect, tex_lookup, extra=None):
    """Returns (height, width, 4) uint8: road colour in rgb, painted amount in alpha; row 0 is the north edge."""
    t0 = time.time()
    x0, y0, x1, y1 = rect
    w, h = (x1 - x0) * GRID + 1, (y1 - y0) * GRID + 1
    ltex, txst, lands = scan(world)
    roads = classify(ltex, txst, tex_lookup, extra)
    amount = np.zeros((h, w), "f4")
    colour = np.zeros((h, w, 3), "f4")
    used = {}
    base_col = np.array([0.42, 0.40, 0.38], "f4")
    for (cx, cy), (raw, comp, full, plugin) in lands.items():
        if not (x0 <= cx < x1 and y0 <= cy < y1):
            continue
        body = zlib.decompress(raw[4:]) if comp else raw

        def form(fid):
            i = fid >> 24
            return (full[i] if i < len(full) else plugin), fid & 0xFFFFFF

        base = {}
        layers = {0: [], 1: [], 2: [], 3: []}
        cur = None
        for t, s in M.subs(body):
            if t == b"BTXT" and len(s) >= 8:
                base[s[4]] = form(struct.unpack_from("<I", s)[0])
            elif t == b"ATXT" and len(s) >= 8:
                cur = [struct.unpack_from("<H", s, 6)[0], form(struct.unpack_from("<I", s)[0]), s[4], None]
                layers[s[4]].append(cur)
            elif t == b"VTXT" and cur is not None:
                cur[3] = s
                cur = None
        for q in range(4):
            names = [base.get(q)] + [l[1] for l in layers[q]]
            if not any(n in roads for n in names):
                continue
            a = np.zeros((17, 17), "f4")
            c = np.zeros((17, 17, 3), "f4")
            b = base.get(q)
            if b in roads:
                a[:] = 1.0
                c[:] = roads[b]["colour"] if roads[b]["colour"] is not None else base_col
            for layer in sorted(layers[q], key=lambda l: l[0]):
                s = layer[3]
                if not s or len(s) < 8:
                    continue
                n = len(s) // 8
                rec = np.frombuffer(s, dtype=[("pos", "<u2"), ("pad", "<u2"), ("op", "<f4")], count=n)
                pos = rec["pos"]
                ok = pos < 289
                rows, cols = pos[ok] // 17, pos[ok] % 17
                op = np.zeros((17, 17), "f4")
                op[rows, cols] = np.clip(rec["op"][ok], 0, 1)
                is_road = layer[1] in roads
                if is_road:
                    rgb = roads[layer[1]]["colour"] if roads[layer[1]]["colour"] is not None else base_col
                    c = c * a[..., None] * (1 - op[..., None]) + np.asarray(rgb) * op[..., None]
                    a = a * (1 - op) + op
                    c = np.where(a[..., None] > 1e-4, c / np.maximum(a[..., None], 1e-4), 0)
                    used[layer[1]] = used.get(layer[1], 0) + int((op > 0.3).sum())
                else:
                    a = a * (1 - op)
            # quadrants: 0 SW, 1 SE, 2 NW, 3 NE; rows run south to north, columns west to east
            ox = (cx - x0) * GRID + (16 if q in (1, 3) else 0)
            oy = (cy - y0) * GRID + (16 if q in (2, 3) else 0)
            cur_a = amount[oy:oy + 17, ox:ox + 17]
            amount[oy:oy + 17, ox:ox + 17] = np.maximum(cur_a, a)
            pick = a >= cur_a
            colour[oy:oy + 17, ox:ox + 17][pick] = c[pick]
    mean = colour[amount > 0.3].mean(0) if (amount > 0.3).any() else base_col
    colour[amount <= 1e-3] = mean
    img = np.zeros((h, w, 4), np.uint8)
    img[..., :3] = np.clip(colour * 255, 0, 255).astype(np.uint8)
    img[..., 3] = np.clip(amount * 255, 0, 255).astype(np.uint8)
    stats = dict(cells=sum(1 for k in lands if x0 <= k[0] < x1 and y0 <= k[1] < y1), road_textures=len(roads), painted_points=int((amount > 0.45).sum()),
                 used=sorted(((roads[k]["label"][:50], v) for k, v in used.items()), key=lambda kv: -kv[1])[:14], seconds=round(time.time() - t0, 1))
    return img[::-1], stats


if __name__ == "__main__":
    from PIL import Image
    import render_map

    world = sys.argv[1]
    rect = tuple(int(v) for v in sys.argv[2:6])
    tex = render_map.Textures()
    img, st = build(world, rect, render_map.mean_colour_lookup(tex))
    tex.save()
    print(st)
    Image.fromarray(img[..., 3]).save(sys.argv[6])
