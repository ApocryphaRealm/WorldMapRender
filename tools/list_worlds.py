"""Every worldspace of the list's active load order with a usable map, by the test World Map Selector for Controller uses:
it owns its map data (no parent it takes map data from) and its NW and SE map cells differ. For each, what LOD exists.

    list_worlds.py out.json
"""
import json
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import markers as M  # noqa: E402

XLOD = os.path.join(M.MODS, "xLODGen Output", "meshes", "terrain")
DYN = os.path.join(M.MODS, "DynDOLOD Output", "meshes", "Terrain")
USE_MAP_DATA = 0x04   # PNAM bit: take map data from the parent


def scan_plugin(path, worlds):
    d = open(path, "rb").read()
    if d[:4] != b"TES4":
        return
    size = struct.unpack_from("<I", d, 4)[0]
    masters = [s[:-1].decode("latin1").lower() for t, s in M.subs(d[24:24 + size]) if t == b"MAST"]
    plugin = os.path.basename(path).lower()
    full_masters = masters + [plugin]

    def owner(fid):
        i = fid >> 24
        return (full_masters[i] if i < len(full_masters) else plugin), fid & 0xFFFFFF

    p = 24 + size
    while p + 24 <= len(d):
        gs = struct.unpack_from("<I", d, p + 4)[0]
        if d[p + 8:p + 12] == b"WRLD":
            q = p + 24
            end = p + gs
            while q < end:
                if d[q:q + 4] == b"GRUP":
                    q += struct.unpack_from("<I", d, q + 4)[0]
                    continue
                sz, fl, fid = struct.unpack_from("<III", d, q + 4)
                body = d[q + 24:q + 24 + sz]
                if fl & 0x40000:
                    body = zlib.decompress(body[4:])
                r = {"plugin": os.path.basename(path), "deleted": bool(fl & 0x20), "parent": None, "parent_flags": 0}
                for t, s in M.subs(body):
                    if t == b"EDID":
                        r["edid"] = s[:-1].decode("latin1")
                    elif t == b"MNAM" and len(s) >= 16:
                        r["mnam"] = struct.unpack_from("<2i4h", s)
                        if len(s) >= 28:
                            r["cam"] = struct.unpack_from("<3f", s, 16)
                    elif t == b"ONAM" and len(s) >= 16:
                        r["onam"] = struct.unpack_from("<4f", s)
                    elif t == b"NAM0" and len(s) >= 8:
                        r["sw"] = struct.unpack_from("<2f", s)
                    elif t == b"NAM9" and len(s) >= 8:
                        r["ne"] = struct.unpack_from("<2f", s)
                    elif t == b"WNAM" and len(s) == 4:
                        r["parent"] = owner(struct.unpack_from("<I", s)[0])
                    elif t == b"PNAM" and len(s) >= 2:
                        r["parent_flags"] = struct.unpack_from("<H", s)[0]
                    elif t == b"FULL":
                        if len(s) == 4:
                            r["full_id"] = struct.unpack_from("<I", s)[0]
                        else:
                            r["full"] = s.rstrip(b"\0").decode("latin1", "replace")
                # later plugins replace earlier ones, as the game does
                worlds[owner(fid)] = {**worlds.get(owner(fid), {}), **{k: v for k, v in r.items() if k not in ("full", "full_id") or v}}
                if "mnam" not in r:
                    worlds[owner(fid)].pop("mnam", None)
                q += 24 + sz
        p += gs


def collect():
    mods = M.active_mods()
    worlds = {}
    for name in M.active_plugins():
        path = M.find_plugin(name, mods)
        if path:
            try:
                scan_plugin(path, worlds)
            except Exception as e:
                print("skip", name, e)
    return worlds, mods


def lod_info(edid, mods):
    low = edid.lower()
    info = {"xlod_level4": 0, "objects": 0, "other_mod": None}
    d = os.path.join(XLOD, edid)
    if os.path.isdir(d):
        names = os.listdir(d)
        info["xlod_level4"] = len([f for f in names if f.lower().startswith(low + ".4.") and f.lower().endswith(".btr") and ".win." not in f.lower()])
    o = os.path.join(DYN, edid, "Objects")
    if os.path.isdir(o):
        info["objects"] = len([f for f in os.listdir(o) if f.lower().endswith(".bto") and f.lower().startswith(low + ".4.")])
    if not info["xlod_level4"]:
        for m in mods:
            dd = os.path.join(M.MODS, m, "meshes", "terrain", edid)
            if os.path.isdir(dd):
                c = [f for f in os.listdir(dd) if f.lower().endswith(".btr")]
                if c:
                    info["other_mod"] = "%s (%d btr)" % (m, len(c))
                    break
    return info


def main():
    worlds, mods = collect()
    out = []
    for key, r in worlds.items():
        if r.get("deleted") or "edid" not in r:
            continue
        has_parent_map = r.get("parent") and (r.get("parent_flags", 0) & USE_MAP_DATA)
        mn = r.get("mnam")
        usable = bool(mn) and (mn[2] != mn[4] or mn[3] != mn[5])
        if has_parent_map or not usable:
            continue
        rec = {"edid": r["edid"], "name": r.get("full") or "", "plugin": r["plugin"], "mnam": mn, "cam": r.get("cam"), "onam": r.get("onam"), "sw": r.get("sw"), "ne": r.get("ne")}
        rec.update(lod_info(r["edid"], mods))
        out.append(rec)
    out.sort(key=lambda x: x["edid"].lower())
    json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), indent=1)
    for r in out:
        print("%-32s l4=%-5d obj=%-4d %-30s last=%-40s other=%s" % (r["edid"], r["xlod_level4"], r["objects"], r["name"][:30], r["plugin"][:40], r["other_mod"]))
    print(len(out), "worlds with usable map data")


if __name__ == "__main__":
    main()
