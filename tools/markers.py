"""Map markers of a worldspace, from every active plugin of the list (last plugin wins).

    markers.py Tamriel out.json

Writes [{"x", "y", "z", "type", "name", "form"}]. Type is the marker's TNAM value: 1 city, 2 town, 3 settlement, ...
"""
import json
import os
import re
import struct
import sys
import time
import zlib

MODS = r"D:\Modlists\Njordlinger\mods"
STOCK = r"D:\Modlists\Njordlinger\Stock Game\Data"
# The one place that says which profile's load order every tool reads. Set WMR_PROFILE to read another.
PROFILE = os.path.join(r"D:\Modlists\Njordlinger\profiles", os.environ.get("WMR_PROFILE", "Njordlinger Test"))


def active_plugins():
    """Skyrim's own masters first, then plugins.txt in order."""
    out = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]
    for line in open(os.path.join(PROFILE, "plugins.txt"), encoding="utf-8", errors="replace"):
        line = line.strip()
        if line.startswith("*"):
            n = line[1:]
            if n.lower() not in [o.lower() for o in out]:
                out.append(n)
        elif line and not line.startswith("#") and line.lower().endswith(".esm") and line.lower() not in [o.lower() for o in out]:
            out.append(line)  # masters listed without the star are active too in older files
    return out


def active_mods():
    out = []
    for line in open(os.path.join(PROFILE, "modlist.txt"), encoding="utf-8", errors="replace"):
        if line.startswith("+"):
            out.append(line[1:].rstrip("\r\n"))
    return out


def find_plugin(name, mods):
    for m in mods:
        p = os.path.join(MODS, m, name)
        if os.path.isfile(p):
            return p
    p = os.path.join(STOCK, name)
    return p if os.path.isfile(p) else None


def subs(body):
    j = 0
    n = len(body)
    while j + 6 <= n:
        t = body[j:j + 4]
        ln = struct.unpack_from("<H", body, j + 4)[0]
        j += 6
        if t == b"XXXX":
            big = struct.unpack_from("<I", body, j)[0]
            j += ln
            t = body[j:j + 4]
            j += 6
            ln = big
        yield t, body[j:j + ln]
        j += ln


def read_plugin(path, target_lower):
    d = open(path, "rb").read()
    if d[:4] != b"TES4":
        return {}
    size = struct.unpack_from("<I", d, 4)[0]
    flags = struct.unpack_from("<I", d, 8)[0]
    hdr = d[24:24 + size]
    masters = [s[:-1].decode("latin1").lower() for t, s in subs(hdr) if t == b"MAST"]
    plugin = os.path.basename(path).lower()
    full = masters + [plugin]

    def form(fid):
        idx = fid >> 24
        owner = full[idx] if idx < len(full) else plugin
        return owner, fid & 0xFFFFFF

    # Find the WRLD top group
    p = 24 + size
    n = len(d)
    wrld_names = {}
    out = {}
    while p + 24 <= n:
        assert d[p:p + 4] == b"GRUP", path
        gsize = struct.unpack_from("<I", d, p + 4)[0]
        label = d[p + 8:p + 12]
        if label == b"WRLD":
            walk(d, p + 24, p + gsize, form, None, wrld_names, out, target_lower)
        p += gsize
    return out


# Markarth's world takes no land from its parent but stands in Tamriel's coordinates: its bounds hold Markarth's own marker.
ALSO_IN_PARENT = {"markarthworld"}
NOT_A_PLACE = re.compile(r"dummy|test", re.I)


def uses_parent_map(body):
    """A child world is in its parent's coordinates if it takes its parent's land (PNAM Use Land Data): the walled cities. Use Map
    Data alone proves nothing: Blackreach, Sovngarde and Dimfrost carry it and are elsewhere altogether."""
    ed, fl = "", 0
    for t, s in subs(body):
        if t == b"EDID":
            ed = s[:-1].decode("latin1")
        elif t == b"PNAM" and len(s) >= 2:
            fl = struct.unpack_from("<H", s)[0]
    if NOT_A_PLACE.search(ed):
        return False
    return bool(fl & 0x01) or ed.lower() in ALSO_IN_PARENT


parents = {}
water_levels = {}  # world's editor ID, lower case: its default water height; the last plugin wins


def walk(d, p, end, form, cur, names, out, target):
    while p + 24 <= end:
        if d[p:p + 4] == b"GRUP":
            gsize = struct.unpack_from("<I", d, p + 4)[0]
            gtype = struct.unpack_from("<i", d, p + 12)[0]
            c = cur
            if gtype == 1:
                lab = struct.unpack_from("<I", d, p + 8)[0]
                c = form(lab)
            if gtype == 9:      # temporary children of a cell: map markers are persistent
                p += gsize
                continue
            if gtype == 1 and target not in (names.get(c), parents.get(c)):
                p += gsize
                continue
            walk(d, p + 24, p + gsize, form, c, names, out, target)
            p += gsize
            continue
        rt = d[p:p + 4]
        sz = struct.unpack_from("<I", d, p + 4)[0]
        fl = struct.unpack_from("<I", d, p + 8)[0]
        fid = struct.unpack_from("<I", d, p + 12)[0]
        if rt == b"WRLD":
            body = d[p + 24:p + 24 + sz]
            if fl & 0x40000:
                body = zlib.decompress(body[4:])
            for t, s in subs(body):
                if t == b"EDID":
                    names[form(fid)] = s.rstrip(b"\0").decode("latin1").lower()
                elif t == b"DNAM" and len(s) >= 8 and form(fid) in names:
                    water_levels[names[form(fid)]] = struct.unpack_from("<f", s, 4)[0]
                elif t == b"WNAM" and len(s) == 4 and (target == "tamriel" or uses_parent_map(body)):
                    # a walled city's own worldspace: its markers stand on the parent's map, in the parent's coordinates
                    parents[form(fid)] = names.get(form(struct.unpack_from("<I", s)[0]))
        elif rt == b"REFR" and cur is not None:
            body = d[p + 24:p + 24 + sz]
            if fl & 0x40000:
                body = zlib.decompress(body[4:])
            if b"XMRK" in body:
                rec = {"form": "%s:%06X" % form(fid), "deleted": bool(fl & 0x20)}
                for t, s in subs(body):
                    if t == b"FULL" and len(s) >= 4:
                        rec["full"] = s[:-1].decode("latin1") if s.endswith(b"\0") else struct.unpack_from("<I", s)[0]
                    elif t == b"TNAM" and len(s) >= 2:
                        rec["type"] = s[1] if s[1] != 0 else s[0]
                        rec["tnam"] = s[:2].hex()
                    elif t == b"DATA" and len(s) >= 12:
                        rec["x"], rec["y"], rec["z"] = struct.unpack_from("<3f", s)
                if "x" in rec and "type" in rec:
                    out[rec["form"]] = rec
        p += 24 + sz


def collect(world):
    mods = active_mods()
    all_ = {}
    t0 = time.time()
    count = 0
    for name in active_plugins():
        path = find_plugin(name, mods)
        if not path:
            continue
        try:
            found = read_plugin(path, world.lower())
        except Exception as e:
            print("skip", name, e)
            continue
        count += 1
        all_.update(found)
    print("plugins read", count, "markers", len(all_), "seconds", round(time.time() - t0, 1))
    return [v for v in all_.values() if not v.get("deleted")]


if __name__ == "__main__":
    res = collect(sys.argv[1])
    json.dump(res, open(sys.argv[2], "w", encoding="utf-8"))
    types = {}
    for r in res:
        types[r["type"]] = types.get(r["type"], 0) + 1
    print(sorted(types.items()))
