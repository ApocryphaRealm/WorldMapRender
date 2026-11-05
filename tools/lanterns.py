"""Every Lanterns of Skyrim light placed in a worldspace, from the active plugins that master the lantern plugin (last one wins).

    lanterns.py Tamriel out.json

Writes [[x, y, base editor ID], ...], enabled references only, one per spot: a lantern is often a mesh and a light standing together.
Written so: plugins without the lantern plugin as a master cannot place or change these references, so only those are read.
"""
import json
import os
import re
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import markers as M  # noqa: E402

LANTERN_PLUGIN = "lanterns of skyrim ii.esm"
FLAG_DELETED, FLAG_DISABLED, FLAG_COMPRESSED = 0x20, 0x800, 0x40000
NOT_A_LIGHT = re.compile(r"insect|swarm|static_pole|_pole|_hang", re.I)  # the plugin's other placed things: bugs round a lamp, bare posts


def header(d):
    size = struct.unpack_from("<I", d, 4)[0]
    masters = [s[:-1].decode("latin1").lower() for t, s in M.subs(d[24:24 + size]) if t == b"MAST"]
    return size, masters


def bases(d, size):
    """Local form ID -> editor ID of what the lantern plugin itself defines, placed-object types only."""
    out = {}
    p = 24 + size
    while p + 24 <= len(d):
        gs = struct.unpack_from("<I", d, p + 4)[0]
        label = d[p + 8:p + 12]
        if label in (b"LIGH", b"STAT", b"MSTT", b"ACTI", b"FURN"):
            q = p + 24
            while q < p + gs:
                sz, fl, fid = struct.unpack_from("<III", d, q + 4)
                body = d[q + 24:q + 24 + sz]
                if fl & FLAG_COMPRESSED:
                    body = zlib.decompress(body[4:])
                for t, s in M.subs(body):
                    if t == b"EDID":
                        out[fid & 0xFFFFFF] = (label.decode(), s[:-1].decode("latin1"))
                        break
                q += 24 + sz
        p += gs
    return out


def read_plugin(path, target, known, placed):
    d = open(path, "rb").read()
    if d[:4] != b"TES4":
        return
    size, masters = header(d)
    plugin = os.path.basename(path).lower()
    full = masters + [plugin]

    def form(fid):
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
                    base, pos = None, None
                    for t, s in M.subs(body):
                        if t == b"NAME" and len(s) == 4:
                            base = form(struct.unpack_from("<I", s)[0])
                        elif t == b"DATA" and len(s) >= 12:
                            pos = struct.unpack_from("<2f", s)
                    if base and base[0] == LANTERN_PLUGIN and base[1] in known:
                        key = form(fid)
                        if fl & (FLAG_DELETED | FLAG_DISABLED) or NOT_A_LIGHT.search(known[base[1]][1]):
                            placed.pop(key, None)
                        elif pos:
                            placed[key] = (pos[0], pos[1], known[base[1]][1], known[base[1]][0])
                    elif base and form(fid) in placed:
                        placed.pop(form(fid), None)  # the override made it something else
            p += 24 + sz

    p = 24 + size
    while p + 24 <= len(d):
        gs = struct.unpack_from("<I", d, p + 4)[0]
        if d[p + 8:p + 12] == b"WRLD":
            walk(p + 24, p + gs, None)
        p += gs


def collect(world):
    t0 = time.time()
    mods = M.active_mods()
    target = world.lower()
    src = None
    for name in M.active_plugins():
        if name.lower() == LANTERN_PLUGIN:
            src = M.find_plugin(name, mods)
    if not src:
        return []
    d = open(src, "rb").read()
    size, _ = header(d)
    known = bases(d, size)
    placed = {}
    count = 0
    for name in M.active_plugins():
        path = M.find_plugin(name, mods)
        if not path:
            continue
        with open(path, "rb") as f:
            h = f.read(65536)
        if h[:4] != b"TES4":
            continue
        if name.lower() != LANTERN_PLUGIN and LANTERN_PLUGIN.encode() not in h.lower():
            continue
        read_plugin(path, target, known, placed)
        count += 1
    # one speck per spot
    spots = {}
    for x, y, ed, kind in placed.values():
        spots.setdefault((round(x / 96), round(y / 96)), (x, y, ed, kind))
    out = list(spots.values())
    print("lantern plugins read", count, "references", len(placed), "spots", len(out), "seconds", round(time.time() - t0, 1), flush=True)
    return out


if __name__ == "__main__":
    res = collect(sys.argv[1])
    kinds = {}
    for r in res:
        kinds[r[2]] = kinds.get(r[2], 0) + 1
    print(sorted(kinds.items(), key=lambda kv: -kv[1])[:25])
    json.dump(res, open(sys.argv[2], "w", encoding="utf-8"))
