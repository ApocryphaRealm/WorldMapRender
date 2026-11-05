"""World Map Render's rerun: render every land in worlds.json from the list's current LOD and write the mod's data files.

    build_maps.py --out <stage folder> [--world Tamriel] [--ppc 128] [--preview <folder>]

For each land: reads the map markers, renders day and night straight down, compresses them to DDS, writes the sheet's mesh
under all five season names, and adds the land's line to SKSE/Plugins/WorldMapRender.txt. Run it after xLODGen and DynDOLOD
whenever the list's landscape changes; nothing in the modlist's profile is touched. A land already written keeps its files
unless named with --world or --all is given.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lanterns as lanterns_mod  # noqa: E402
import lights as lights_mod  # noqa: E402
import markers as markers_mod  # noqa: E402
import nifread  # noqa: E402
import nifwrite  # noqa: E402
import render_map  # noqa: E402

CELL = 4096.0
SHEET_Z = 180000.0
BACK_Z = 162000.0
LAND_Z = 160000.0
HMIN = 242000.0
CLOSEST = 56000.0
WNEAR = 28000.0
SEASONS = ("", ".AUT", ".SPR", ".SUM", ".WIN")
MAX_SIDE = 16384
TEXCONV_PLACES = [
    r"D:\Modlists\Njordlinger\tools\DynDOLOD\Edit Scripts\Texconvx64.exe",
]
# What the edge of the sheet fades to, and the backdrop under it: the map's own dark sea.
EDGE_DAY = np.array([33, 41, 52], np.float32)
EDGE_NIGHT = np.array([10, 13, 20], np.float32)


def texconv():
    for p in TEXCONV_PLACES:
        if os.path.isfile(p):
            return p
    w = shutil.which("texconv")
    if w:
        return w
    sys.exit("texconv not found")


def fade_edges(img, width, colour):
    """A soft edge, so the sheet ends in the backdrop rather than in a cut line."""
    h, w = img.shape[:2]
    x = np.minimum(np.arange(w), np.arange(w)[::-1]).astype(np.float32) / width
    y = np.minimum(np.arange(h), np.arange(h)[::-1]).astype(np.float32) / width
    fx = np.clip(x, 0, 1)
    fy = np.clip(y, 0, 1)
    out = img
    # one strip at a time: the middle of the picture does not need the memory
    b = int(width) + 1
    if b * 2 >= min(h, w):
        b = min(h, w) // 2
    def strip(sl):
        a = out[sl].astype(np.float32)
        m = (fy[sl[0]][:, None] * fx[sl[1]][None, :])[..., None]
        m = m * m * (3 - 2 * m)
        out[sl] = (a * m + colour * (1 - m)).astype(np.uint8)
    full = slice(None)
    strip((slice(0, b), full))
    strip((slice(h - b, h), full))
    strip((slice(b, h - b), slice(0, b)))
    strip((slice(b, h - b), slice(w - b, w)))
    return out


def to_dds(img, dest, tc):
    """BC1 with a full mip chain. Pictures go through a temporary PNG beside the output."""
    tmp = dest + ".png"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    Image.fromarray(img).save(tmp, compress_level=1)
    d = os.path.dirname(dest)
    r = subprocess.run([tc, "-nologo", "-y", "-f", "BC1_UNORM", "-m", "0", "-if", "BOX", "-o", d, tmp], capture_output=True, text=True)
    made = tmp[:-4] + ".dds"  # texconv replaces the last extension
    if os.path.isfile(made):
        os.replace(made, dest)
    os.remove(tmp)
    if r.returncode != 0 or not os.path.isfile(dest):
        sys.exit("texconv failed on %s: %s" % (dest, r.stdout[-400:] + r.stderr[-400:]))


def marker_json(out, cfg):
    """FWMF's whitelist of lands whose markers stand at the sheet's height, with every land of ours added."""
    src = os.path.join(render_map.MODS, "Flat World Map Framework (FWMF) [Patch]", "SKSE", "Plugins", "FlatMapMarkersSSE.json")
    data = json.load(open(src, encoding="utf-8-sig")) if os.path.isfile(src) else {"whiteListedWorldSpaces": []}
    have = {e["editorID"].lower(): e for e in data["whiteListedWorldSpaces"]}
    for edid, c in cfg.items():
        e = have.get(edid.lower())
        if e:
            e["markerHeight"] = int(SHEET_Z)
            e["enabled"] = True
        else:
            data["whiteListedWorldSpaces"].append({"name": c["name"], "editorID": edid, "markerHeight": int(SHEET_Z), "enabled": True})
    with open(os.path.join(out, "SKSE", "Plugins", "FlatMapMarkersSSE.json"), "w", encoding="utf-8", newline="\r\n") as f:
        json.dump(data, f, indent="\t")


def table_line(edid, rect, cfg):
    x0, y0, x1, y1 = rect
    usable = cfg.get("usable", [0, 0])
    sx0, sx1, sy0, sy1 = x0 * CELL, x1 * CELL, y0 * CELL, y1 * CELL
    wfar = max(sx1 - sx0, (sy1 - sy0) * 16.0 / 9.0) * 1.02
    wfar = min(wfar, (sx1 - sx0))  # never wider than the sheet: the view stays on the map
    wfar = max(wfar, WNEAR * 1.5)
    hmax = max(HMIN + 40000.0, wfar / 2.0 - CLOSEST + HMIN)
    return "%s %.1f %.1f %.1f %.1f %.1f %d %d %d %d %d %d %d 0.0 0.0 0.0 %s %.1f %.1f %.1f %.1f" % (
        edid, SHEET_Z, HMIN, hmax, wfar, WNEAR, 0 if cfg.get("single") else 1, usable[0], usable[1], x0, y1, x1, y0, cfg.get("onamz", 184072.96875), sx0, sx1, sy0, sy1)


def auto_rect(edid, mk):
    """Where the land's content is: its object LOD tiles and its markers, with a margin; whole cells. A land with no object LOD
    (Beyond Skyrim's Cyrodiil here) uses the terrain standing above water instead. Terrain alone is not used where objects exist,
    because the LOD also carries miles of empty border."""
    xs, ys = [], []
    o = os.path.join(render_map.MODS, "DynDOLOD Output", "meshes", "Terrain", edid, "Objects")
    for f in glob.glob(os.path.join(o, "*.4.*.bto")):
        m = re.match(r".*\.4\.(-?\d+)\.(-?\d+)\.bto$", f, re.I)
        if m:
            xs += [int(m.group(1)), int(m.group(1)) + 4]
            ys += [int(m.group(2)), int(m.group(2)) + 4]
    if not xs:
        d = os.path.join(render_map.MODS, "xLODGen Output", "meshes", "terrain", edid)
        for f in glob.glob(os.path.join(d, "*.4.*.btr")):
            m = re.match(r".*\.4\.(-?\d+)\.(-?\d+)\.btr$", f, re.I)
            if not m:
                continue
            shapes = nifread.parse(f)
            land = [sh for sh in shapes if sh.name.lower() == "land" and sh.verts is not None and len(sh.verts)]
            water = [float(sh.verts[:, 2].mean()) for sh in shapes if sh.name.lower() != "land" and sh.verts is not None and len(sh.verts)]
            if not land or (water and float(land[0].verts[:, 2].max()) <= max(water) + 20.0):
                continue
            xs += [int(m.group(1)), int(m.group(1)) + 4]
            ys += [int(m.group(2)), int(m.group(2)) + 4]
    for r in mk:
        xs += [int(r["x"] // CELL) - 2, int(r["x"] // CELL) + 3]
        ys += [int(r["y"] // CELL) - 2, int(r["y"] // CELL) + 3]
    if not xs:
        return None
    return (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2)


def resolve_host(cfg, rect, edid):
    if cfg.get("host") != "auto":
        return tuple(cfg["host"])
    d = os.path.join(render_map.MODS, "xLODGen Output", "meshes", "terrain", edid)
    tiles = []
    for f in glob.glob(os.path.join(d, "*.32.*.btr")):
        m = re.match(r".*\.32\.(-?\d+)\.(-?\d+)\.btr$", f, re.I)
        if m:
            tiles.append((int(m.group(1)), int(m.group(2))))
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    for tx, ty in tiles:
        if tx <= cx < tx + 32 and ty <= cy < ty + 32:
            return tx, ty
    return tiles[0] if tiles else (0, 0)


def build_world(edid, cfg, out, ppc, tc, preview):
    wd = render_map.world_from_config(edid, cfg)
    t0 = time.time()
    mk = markers_mod.collect(edid)
    t_begin = time.time()
    rect = auto_rect(edid, mk) if cfg["rect"] == "auto" else tuple(cfg["rect"])
    if not rect:
        raise SystemExit("no land found for %s" % edid)
    cfg["_rect"] = rect
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    use = min(ppc, MAX_SIDE // max(w, h))
    # both sides must be multiples of four cells' worth of block compression
    t1 = time.time()
    # "sea": true in worlds.json for a land with an open sea; elsewhere the default water level would flood its valleys
    sea = markers_mod.water_levels.get(edid.lower()) if cfg.get("sea", False) else None
    if sea is not None and sea < -1.0e6:
        sea = None
    day, night, stats, timing = render_map.render(wd, rect, use, mk, sea, lanterns_mod.collect(edid), lights_mod.collect(edid) if cfg.get("lights") else ())
    t2 = time.time()
    day = fade_edges(day.copy(), use * 1.5, EDGE_DAY)
    night = fade_edges(night.copy(), use * 1.5, EDGE_NIGHT)
    low = edid.lower()
    tdir = os.path.join(out, "textures", "terrain", "worldmaprender")
    if preview:
        os.makedirs(preview, exist_ok=True)
        for tag, img in (("day", day), ("night", night)):
            im = Image.fromarray(img)
            scale = max(1, max(im.size) // 2048)
            im.reduce(scale).save(os.path.join(preview, "%s-%s.png" % (low, tag)))
    # "single": "night" or "day": a land with no sky cycle has one picture, and the plugin never swaps it
    single = cfg.get("single")
    to_dds(night if single == "night" else day, os.path.join(tdir, low + ".dds"), tc)
    if not single:
        to_dds(night, os.path.join(tdir, low + "_n.dds"), tc)
    elif os.path.isfile(os.path.join(tdir, low + "_n.dds")):
        os.remove(os.path.join(tdir, low + "_n.dds"))
    t3 = time.time()
    # the mesh: its numbers are relative to the host chunk's corner
    hx, hy = resolve_host(cfg, rect, edid)
    ox, oy = hx * CELL, hy * CELL
    x0, y0, x1, y1 = rect
    sheet_tex = "textures\\terrain\\worldmaprender\\%s.dds" % low
    back_tex = "textures\\terrain\\worldmaprender\\backdrop.dds"
    data = nifwrite.sheet_btr(sheet_tex, back_tex, x0 * CELL - ox, x1 * CELL - ox, y0 * CELL - oy, y1 * CELL - oy, SHEET_Z, BACK_Z, LAND_Z,
                              ((x0 + x1) / 2 * CELL - ox, (y0 + y1) / 2 * CELL - oy))
    mdir = os.path.join(out, "meshes", "terrain", edid)
    os.makedirs(mdir, exist_ok=True)
    for s in SEASONS:
        with open(os.path.join(mdir, "%s.32.%d.%d%s.btr" % (low, hx, hy, s)), "wb") as f:
            f.write(data)
    return dict(world=edid, sea=sea, rect=list(rect), host=[hx, hy], size=[day.shape[1], day.shape[0]], ppc=use, markers=len(mk), stats={k: v for k, v in stats.items() if k != "missing"}, missing=stats["missing"],
                timing=dict(render=round(t2 - t1, 1), markers=round(t1 - t0, 1), compress=round(t3 - t2, 1)), dds_mb=round(os.path.getsize(os.path.join(tdir, low + ".dds")) / 1e6, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--world", action="append")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ppc", type=int, default=128)
    ap.add_argument("--preview")
    args = ap.parse_args()
    cfg = {k: v for k, v in json.load(open(os.path.join(HERE, "worlds.json"), encoding="utf-8")).items() if not k.startswith("_")}
    names = args.world or list(cfg)
    tc = texconv()
    os.makedirs(os.path.join(args.out, "SKSE", "Plugins"), exist_ok=True)
    tdir = os.path.join(args.out, "textures", "terrain", "worldmaprender")
    os.makedirs(tdir, exist_ok=True)
    back = os.path.join(tdir, "backdrop.dds")
    if not os.path.isfile(back):
        im = np.tile(EDGE_NIGHT.astype(np.uint8), (64, 64, 1))
        to_dds(im, back, tc)
    report_path = os.path.join(args.out, "..", "build-report.json")
    report = json.load(open(report_path, encoding="utf-8")) if os.path.isfile(report_path) else {}
    for edid in names:
        if edid not in cfg:
            sys.exit("%s is not in worlds.json" % edid)
        low = edid.lower()
        if not args.all and not args.world and os.path.isfile(os.path.join(tdir, low + ".dds")):
            print("kept", edid)
            continue
        print("building", edid, flush=True)
        try:
            report[edid] = build_world(edid, cfg[edid], args.out, args.ppc, tc, args.preview)
        except (Exception, SystemExit) as e:
            # one land's failure must not cost the others; it stays on its old map, and the report says why
            report[edid] = {"failed": str(e)[:300]}
            print("FAILED", edid, e, flush=True)
            continue
        print(json.dumps(report[edid])[:700], flush=True)
        json.dump(report, open(report_path, "w", encoding="utf-8"), indent=1)
    report_rects = {k: v.get("rect") for k, v in report.items()}
    json.dump(report, open(report_path, "w", encoding="utf-8"), indent=1)
    for edid, c in cfg.items():
        if c["rect"] == "auto" and report_rects.get(edid):
            c["rect"] = report_rects[edid]
    marker_json(args.out, cfg)
    # The table lists every land whose picture is present, so a partial rerun cannot lose the rest.
    lines = ["# World Map Render. Written by tools/build_maps.py. edid z hmin hmax wfar wnear night usableW usableH nwX nwY seX seY onamScale onamX onamY onamZ sheetX0 sheetX1 sheetY0 sheetY1",
             "# @ closest panNear panRange keys",
             "@ %.1f 20000.0 60000.0 0.00714" % CLOSEST]
    for edid, c in cfg.items():
        if os.path.isfile(os.path.join(tdir, edid.lower() + ".dds")) and c["rect"] != "auto":
            lines.append(table_line(edid, tuple(c["rect"]), c))
    with open(os.path.join(args.out, "SKSE", "Plugins", "WorldMapRender.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    print("table lands:", len(lines) - 3)


if __name__ == "__main__":
    main()
