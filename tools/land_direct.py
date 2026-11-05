"""Terrain and buildings drawn straight from a land's records, for a land whose LOD is poor or missing.

land_images(): each cell's winning LAND becomes heights and a colour, 32 texels a cell, by the same paint layers the game blends.
placed_models(): every large static placed in the land or its child worlds (a walled city's world), with its full model.
"""
import math
import os
import re
import struct
import sys
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import markers as M  # noqa: E402
import nifread  # noqa: E402
import roads  # noqa: E402

GRID = 32
FLAG_COMPRESSED = 0x40000
MIN_SIZE = 280.0   # units across: smaller things do not show on a map
SKIP_MODEL = re.compile(r"\bfx|effects[\\/]|marker|collision|_lod|\\lod\\|water|sky|cloud|mist|fog", re.I)


def child_worlds(world):
    """Editor IDs of the worlds whose WNAM parent is this one: a walled city and the like, in its parent's coordinates."""
    target = world.lower()
    mods = M.active_mods()
    names, parents, flags = {}, {}, {}
    for name in M.active_plugins():
        path = M.find_plugin(name, mods)
        if not path:
            continue
        d = open(path, "rb").read()
        if d[:4] != b"TES4":
            continue
        size = struct.unpack_from("<I", d, 4)[0]
        masters = [s[:-1].decode("latin1").lower() for t, s in M.subs(d[24:24 + size]) if t == b"MAST"]
        full = masters + [name.lower()]

        def form(fid):
            i = fid >> 24
            return (full[i] if i < len(full) else name.lower()), fid & 0xFFFFFF

        p = 24 + size
        while p + 24 <= len(d):
            gs = struct.unpack_from("<I", d, p + 4)[0]
            if d[p + 8:p + 12] == b"WRLD":
                q = p + 24
                while q < p + gs:
                    if d[q:q + 4] == b"GRUP":
                        q += struct.unpack_from("<I", d, q + 4)[0]
                        continue
                    sz, fl, fid = struct.unpack_from("<III", d, q + 4)
                    body = d[q + 24:q + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    for t, s in M.subs(body):
                        if t == b"EDID":
                            names[form(fid)] = s[:-1].decode("latin1")
                        elif t == b"WNAM" and len(s) == 4 and M.uses_parent_map(body):
                            parents[form(fid)] = form(struct.unpack_from("<I", s)[0])
                    q += 24 + sz
            p += gs
    return sorted(names[k] for k, par in parents.items() if k in names and names.get(par, "").lower() == target)


def land_images(world, rect, colour_of, fill=None, hide=()):
    x0, y0, x1, y1 = rect
    w, h = (x1 - x0) * GRID + 1, (y1 - y0) * GRID + 1
    ltex, txst, lands = roads.scan(world)
    height = np.full((h, w), np.nan, "f4")
    colour = np.zeros((h, w, 3), "f4")
    grey = np.array([0.35, 0.34, 0.33], "f4")

    def rgb(k):
        r = ltex.get(k)
        c = colour_of(txst.get(r.get("set"), {}).get("tex", "")) if r else None
        return grey if c is None else c

    for (cx, cy), (raw, comp, full, plugin) in lands.items():
        if not (x0 <= cx < x1 and y0 <= cy < y1):
            continue
        if any(h[0] <= cx + 0.5 < h[2] and h[1] <= cy + 0.5 < h[3] for h in hide):
            continue
        body = zlib.decompress(raw[4:]) if comp else raw

        def form(fid):
            i = fid >> 24
            return (full[i] if i < len(full) else plugin), fid & 0xFFFFFF

        base, layers, cur, hgt = {}, {0: [], 1: [], 2: [], 3: []}, None, None
        for t, s in M.subs(body):
            if t == b"VHGT" and len(s) >= 1093:
                hgt = s
            elif t == b"BTXT" and len(s) >= 8:
                base[s[4]] = form(struct.unpack_from("<I", s)[0])
            elif t == b"ATXT" and len(s) >= 8:
                cur = [struct.unpack_from("<H", s, 6)[0], form(struct.unpack_from("<I", s)[0]), s[4], None]
                layers[s[4]].append(cur)
            elif t == b"VTXT" and cur is not None:
                cur[3] = s
                cur = None
        ox, oy = (cx - x0) * GRID, (cy - y0) * GRID
        if hgt is not None:
            off = struct.unpack_from("<f", hgt)[0]
            g = np.frombuffer(hgt, dtype=np.int8, count=1089, offset=4).astype("f4").reshape(33, 33)
            first = np.cumsum(g[:, 0])
            field = np.cumsum(np.concatenate([first[:, None], g[:, 1:]], axis=1), axis=1)
            height[oy:oy + 33, ox:ox + 33] = (off + field) * 8.0
        for q in range(4):
            c = np.tile(rgb(base[q]) if q in base else grey, (17, 17, 1)).astype("f4")
            for layer in sorted(layers[q], key=lambda l: l[0]):
                s = layer[3]
                if not s or len(s) < 8:
                    continue
                rec = np.frombuffer(s, dtype=[("pos", "<u2"), ("pad", "<u2"), ("op", "<f4")], count=len(s) // 8)
                ok = rec["pos"] < 289
                op = np.zeros((17, 17), "f4")
                op[rec["pos"][ok] // 17, rec["pos"][ok] % 17] = np.clip(rec["op"][ok], 0, 1)
                c = c * (1 - op[..., None]) + rgb(layer[1]) * op[..., None]
            qx = ox + (16 if q in (1, 3) else 0)
            qy = oy + (16 if q in (2, 3) else 0)
            colour[qy:qy + 17, qx:qx + 17] = c
    missing = np.isnan(height)
    low = float(np.nanmin(height)) if (~missing).any() else -14000.0
    height[missing] = -50000.0 if fill is None else fill   # cells with no land: the map shaders leave these out
    return height[::-1], colour[::-1], int((~missing).sum() // (33 * 33))


def rotation(rx, ry, rz):
    """Placed reference's rotation, applied to a column vector: Rx(-x) Ry(-y) Rz(-z). Measured, not assumed: of the 12 orders and
    signs, this one puts Skuldafn's models where DynDOLOD's LOD of them stands (median height difference 125 units, against 139 to 655)."""
    cx, sx, cy, sy, cz, sz = math.cos(-rx), math.sin(-rx), math.cos(-ry), math.sin(-ry), math.cos(-rz), math.sin(-rz)
    X = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], "f4")
    Y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], "f4")
    Z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], "f4")
    return X @ Y @ Z


def parse_mods(s, form):
    """A record's alternate textures: [(3D shape name, texture set form)]."""
    out = []
    n = struct.unpack_from("<I", s)[0]
    p = 4
    for _ in range(n):
        ln = struct.unpack_from("<I", s, p)[0]
        name = s[p + 4:p + 4 + ln].decode("latin1", "replace")
        p += 4 + ln
        out.append((name, form(struct.unpack_from("<I", s, p)[0])))
        p += 8
    return out


def placed_models(world, alt_textures=False):
    """{model path: [(x, y, z, rx, ry, rz, scale), ...]} for large statics, enabled and undeleted, in the land and its child worlds.
    With alt_textures the key is "model|shape=texture|...": the record swaps those shapes' diffuse, as the game does."""
    target = world.lower()
    mods = M.active_mods()
    bases, refs, sets = {}, {}, {}
    names, parents = {}, {}   # across plugins: a plugin may override a child world without carrying its parent's record
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

        def walk(p, end, cur):
            while p + 24 <= end:
                if d[p:p + 4] == b"GRUP":
                    gs = struct.unpack_from("<I", d, p + 4)[0]
                    gt = struct.unpack_from("<i", d, p + 12)[0]
                    c = cur
                    if gt == 1:
                        c = form(struct.unpack_from("<I", d, p + 8)[0])
                        if target != names.get(c) and target != names.get(parents.get(c)):
                            p += gs
                            continue
                    walk(p + 24, p + gs, c)
                    p += gs
                    continue
                rt = d[p:p + 4]
                sz, fl, fid = struct.unpack_from("<III", d, p + 4)
                if rt == b"WRLD" or (rt == b"REFR" and cur is not None):
                    body = d[p + 24:p + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    if rt == b"WRLD":
                        for t, s in M.subs(body):
                            if t == b"EDID":
                                names[form(fid)] = s[:-1].decode("latin1").lower()
                            elif t == b"WNAM" and len(s) == 4 and M.uses_parent_map(body):
                                parents[form(fid)] = form(struct.unpack_from("<I", s)[0])
                    else:
                        b, pos, scale = None, None, 1.0
                        for t, s in M.subs(body):
                            if t == b"NAME" and len(s) == 4:
                                b = form(struct.unpack_from("<I", s)[0])
                            elif t == b"DATA" and len(s) >= 24:
                                pos = struct.unpack_from("<6f", s)
                            elif t == b"XSCL" and len(s) == 4:
                                scale = struct.unpack_from("<f", s)[0]
                        key = form(fid)
                        # deleted, initially disabled, or sunk out of sight as some city mods hide what they replace
                        if fl & 0x820 or (pos and pos[2] < -29000.0):
                            refs.pop(key, None)
                        elif b and pos:
                            refs[key] = (b, pos, scale)
                p += 24 + sz

        p = 24 + size
        while p + 24 <= len(d):
            gs = struct.unpack_from("<I", d, p + 4)[0]
            label = d[p + 8:p + 12]
            if label == b"TXST" and alt_textures:
                q = p + 24
                while q < p + gs:
                    sz, fl, fid = struct.unpack_from("<III", d, q + 4)
                    body = d[q + 24:q + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    for t, s in M.subs(body):
                        if t == b"TX00":
                            sets[form(fid)] = s[:-1].decode("latin1")
                            break
                    q += 24 + sz
            elif label in (b"STAT", b"MSTT"):
                q = p + 24
                while q < p + gs:
                    sz, fl, fid = struct.unpack_from("<III", d, q + 4)
                    body = d[q + 24:q + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    model, bound, swaps = None, None, ()
                    for t, s in M.subs(body):
                        if t == b"MODL":
                            model = s[:-1].decode("latin1")
                        elif t == b"OBND" and len(s) == 12:
                            bound = struct.unpack("<6h", s)
                        elif t == b"MODS" and alt_textures and len(s) >= 4:
                            try:
                                swaps = parse_mods(s, form)
                            except struct.error:
                                swaps = ()
                    if model and bound:
                        across = max(bound[3] - bound[0], bound[4] - bound[1])
                        bases[form(fid)] = (model, across, swaps)
                    q += 24 + sz
            elif label == b"WRLD":
                walk(p + 24, p + gs, None)
            p += gs
    out = {}
    for b, pos, scale in refs.values():
        info = bases.get(b)
        if not info or info[1] * scale < MIN_SIZE or SKIP_MODEL.search(info[0]) or "ceiling" in info[0].lower():
            continue
        key = info[0].lower().replace("/", "\\")
        for name, set_form in info[2]:
            if set_form in sets:
                key += "|%s=%s" % (name.lower(), sets[set_form])
        out.setdefault(key, []).append((*pos, scale))
    return out


def load_model(rel, tex):
    """Shapes of a full model: [(verts, uvs, colours, tris, texture path)], in the model's own space."""
    swaps = {}
    if "|" in rel:
        parts = rel.split("|")
        rel = parts[0]
        for p in parts[1:]:
            a, b = p.split("=", 1)
            swaps[a] = b
    path = rel if rel.startswith("meshes\\") else "meshes\\" + rel
    f = tex.find(path)
    data = open(f, "rb").read() if f else tex.archived(path)
    if not data:
        return None
    out = []
    for s in nifread.parse_bytes(data):
        if s.verts is None or s.tris is None or s.uvs is None or not len(s.tris) or not s.textures:
            continue
        if not np.isfinite(s.verts).all():
            continue
        # the diffuse's alpha is often not transparency (specular, in PBR sets), so only a shape that asks for alpha testing gets it
        cut = bool(s.alpha and (s.alpha[0] & 0x200))
        tex_path = swaps.get(s.name.lower(), s.textures[0])
        out.append((s.verts, s.uvs, s.colors, s.tris, tex_path, cut))
    return out
