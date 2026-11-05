"""Everything that gives light in a land, from its references in the winning plugins: light records with their own colour and
radius, and bases named as glowing things (fungi, the crimson nirnroot, Dwemer lamps), coloured by what they are.

    lights.py Blackreach out.json
"""
import json
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import markers as M  # noqa: E402

FLAG_COMPRESSED = 0x40000
KINDS = [  # by editor ID and model name: (name, colour, radius in units)
    ("nirnroot", re.compile(r"nirnroot|crimson", re.I), (0.95, 0.16, 0.22), 170.0),
    ("fungus", re.compile(r"mushroom|shroom|fung|glowplant|glowing.?plant|spore|luminous", re.I), (0.20, 0.86, 0.92), 150.0),
    ("dwemer", re.compile(r"(dwemer|dwarven|dwe).*(lamp|lantern|light|glow)|(lamp|lantern).*(dwemer|dwarven)", re.I), (1.00, 0.72, 0.38), 190.0),
]


def collect(world):
    target = world.lower()
    mods = M.active_mods()
    bases, refs = {}, {}
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

        def walk(p, end, cur):
            while p + 24 <= end:
                if d[p:p + 4] == b"GRUP":
                    gs = struct.unpack_from("<I", d, p + 4)[0]
                    gt = struct.unpack_from("<i", d, p + 12)[0]
                    c = cur
                    if gt == 1:
                        c = form(struct.unpack_from("<I", d, p + 8)[0])
                        if target not in (names.get(c), parents.get(c)):
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
                            elif t == b"WNAM" and len(s) == 4:
                                parents[form(fid)] = names.get(form(struct.unpack_from("<I", s)[0]))
                    else:
                        b, pos = None, None
                        for t, s in M.subs(body):
                            if t == b"NAME" and len(s) == 4:
                                b = form(struct.unpack_from("<I", s)[0])
                            elif t == b"DATA" and len(s) >= 12:
                                pos = struct.unpack_from("<2f", s)
                        key = form(fid)
                        if fl & 0x820:
                            refs.pop(key, None)
                        elif b and pos:
                            refs[key] = (b, pos)
                p += 24 + sz

        p = 24 + size
        while p + 24 <= len(d):
            gs = struct.unpack_from("<I", d, p + 4)[0]
            label = d[p + 8:p + 12]
            if label in (b"LIGH", b"STAT", b"MSTT", b"ACTI", b"FLOR", b"TREE", b"MISC"):
                q = p + 24
                while q < p + gs:
                    sz, fl, fid = struct.unpack_from("<III", d, q + 4)
                    body = d[q + 24:q + 24 + sz]
                    if fl & FLAG_COMPRESSED:
                        body = zlib.decompress(body[4:])
                    ed, model, data = "", "", None
                    for t, s in M.subs(body):
                        if t == b"EDID":
                            ed = s[:-1].decode("latin1")
                        elif t == b"MODL":
                            model = s[:-1].decode("latin1")
                        elif t == b"DATA":
                            data = s
                    if label == b"LIGH":
                        if data and len(data) >= 12:
                            radius = struct.unpack_from("<I", data, 4)[0]
                            bases[form(fid)] = ("light", ed, (data[8] / 255.0, data[9] / 255.0, data[10] / 255.0), min(max(radius * 0.22, 70.0), 420.0))
                    else:
                        for kind, rx, col, rad in KINDS:
                            if rx.search(ed) or rx.search(model):
                                bases[form(fid)] = (kind, ed, col, rad)
                                break
                    q += 24 + sz
            elif label == b"WRLD":
                walk(p + 24, p + gs, None)
            p += gs
    out = []
    counts = {}
    for b, pos in refs.values():
        info = bases.get(b)
        if not info:
            continue
        col = info[2]
        if info[0] == "light":
            # a light's own colour; a very pale one still reads as warm or cool by what is strongest
            if max(col) < 0.05:
                continue
            m = max(col)
            col = tuple(c / m for c in col)
        out.append((pos[0], pos[1], col[0], col[1], col[2], info[3], info[0]))
        counts[info[0]] = counts.get(info[0], 0) + 1
    print("lights", world, counts, flush=True)
    return out


if __name__ == "__main__":
    json.dump(collect(sys.argv[1]), open(sys.argv[2], "w"))
