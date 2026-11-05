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
import retouch
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
# Height the wheel has on every land: at about a tenth a click, some ten clicks from closest to furthest.
ZOOM_RANGE = 100000.0
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
    # The camera's limits are the picture less its soft edge, so neither the fade nor the backdrop can come into view.
    inset = cfg.get("edge_cells", 1.5) * CELL
    sx0, sx1, sy0, sy1 = x0 * CELL + inset, x1 * CELL - inset, y0 * CELL + inset, y1 * CELL - inset
    # Fully zoomed out, the view fits inside those limits on both sides: a land never shows less than a screenful of picture.
    wfar = min(sx1 - sx0, (sy1 - sy0) * 16.0 / 9.0)
    wfar = max(wfar, WNEAR * 1.5)
    hmax = max(HMIN + ZOOM_RANGE, wfar / 2.0 - CLOSEST + HMIN)
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


SLOTS = 64
TILE_CELLS = 4
CHUNK_CELLS = 32


def write_mesh(edid, cfg, rect, out):
    """The sheet's mesh under all five season names; its numbers are relative to the host chunk's corner."""
    low = edid.lower()
    hx, hy = resolve_host(cfg, rect, edid)
    ox, oy = hx * CELL, hy * CELL
    x0, y0, x1, y1 = rect
    sheet_tex = "textures\\terrain\\worldmaprender\\%s.dds" % low
    back_tex = "textures\\terrain\\worldmaprender\\backdrop.dds"
    slots = ["textures\\terrain\\worldmaprender\\slot\\s%02d.dds" % n for n in range(SLOTS)] if cfg.get("tile_ppc") else ()
    data = nifwrite.sheet_btr(sheet_tex, back_tex, x0 * CELL - ox, x1 * CELL - ox, y0 * CELL - oy, y1 * CELL - oy, SHEET_Z, BACK_Z, LAND_Z,
                              ((x0 + x1) / 2 * CELL - ox, (y0 + y1) / 2 * CELL - oy), slots=slots)
    mdir = os.path.join(out, "meshes", "terrain", edid)
    os.makedirs(mdir, exist_ok=True)
    for s in SEASONS:
        with open(os.path.join(mdir, "%s.32.%d.%d%s.btr" % (low, hx, hy, s)), "wb") as f:
            f.write(data)
    return hx, hy


def build_tiles(edid, cfg, rect, out, tc, only=None):
    """Tiles of TILE_CELLS cells at the land's tile_ppc, BC1 with four mips. Rendered in chunks, since one pass stops at 16384. A tile of
    plain sea is not written: the sheet shows. only = (i0, j0, i1, j1) limits it to a window of tiles, for a trial."""
    ppc = cfg["tile_ppc"]
    low = edid.lower()
    t0 = time.time()
    wd = render_map.world_from_config(edid, cfg)
    mk = markers_mod.collect(edid)
    sea = markers_mod.water_levels.get(low) if cfg.get("sea", False) else None
    lant = lanterns_mod.collect(edid)
    lit = lights_mod.collect(edid) if cfg.get("lights") else ()
    x0, y0, x1, y1 = rect
    root = os.path.join(out, "textures", "terrain", "worldmaprender", "t", low)
    wanted = {"day": "tiles", "night": "tiles_n"}
    single = cfg.get("single")
    if single:
        wanted = {single: "tiles"}
    work = os.path.join(out, "..", "tile-work", low)
    written = skipped = 0
    tile_px = TILE_CELLS * ppc
    fade = ppc * 1.5
    nx, ny = (x1 - x0 + TILE_CELLS - 1) // TILE_CELLS, (y1 - y0 + TILE_CELLS - 1) // TILE_CELLS
    per = CHUNK_CELLS // TILE_CELLS
    for cj in range(0, ny, per):
        for ci in range(0, nx, per):
            ti0, tj0, ti1, tj1 = ci, cj, min(ci + per, nx), min(cj + per, ny)
            if only and (ti1 <= only[0] or ti0 >= only[2] or tj1 <= only[1] or tj0 >= only[3]):
                continue
            rx0 = x0 + ti0 * TILE_CELLS
            ry1 = y1 - tj0 * TILE_CELLS
            rx1 = x0 + ti1 * TILE_CELLS
            ry0 = y1 - tj1 * TILE_CELLS
            wd = render_map.world_from_config(edid, cfg)
            day, night, _, _ = render_map.render(wd, (rx0, ry0, rx1, ry1), ppc, mk, sea, lant, lit)
            for tag, folder in wanted.items():
                img = day if tag == "day" else night
                edge = EDGE_DAY if tag == "day" else EDGE_NIGHT
                d = os.path.join(work, folder)
                os.makedirs(d, exist_ok=True)
                os.makedirs(os.path.join(root, folder), exist_ok=True)
                names = []
                for j in range(tj0, tj1):
                    for i in range(ti0, ti1):
                        if only and not (only[0] <= i < only[2] and only[1] <= j < only[3]):
                            continue
                        py, px = (j - tj0) * tile_px, (i - ti0) * tile_px
                        t = img[py:py + tile_px, px:px + tile_px].astype(np.float32)
                        # the sheet's own soft edge, so a tile meets it
                        gx = (i * tile_px + np.arange(tile_px)) / fade
                        gy = (j * tile_px + np.arange(tile_px)) / fade
                        gx2 = ((x1 - x0) * ppc - (i * tile_px + np.arange(tile_px))) / fade
                        gy2 = ((y1 - y0) * ppc - (j * tile_px + np.arange(tile_px))) / fade
                        m = np.clip(np.minimum(gx, gx2), 0, 1)[None, :] * np.clip(np.minimum(gy, gy2), 0, 1)[:, None]
                        if m.min() < 1.0:
                            m = (m * m * (3 - 2 * m))[..., None]
                            t = t * m + edge * (1 - m)
                        if t.std(axis=(0, 1)).max() < 1.0:
                            skipped += 1
                            continue
                        p = os.path.join(d, "t%d_%d.png" % (i, j))
                        Image.fromarray(t.astype(np.uint8)).save(p, compress_level=1)
                        names.append(p)
                if names:
                    lst = os.path.join(d, "list.txt")
                    with open(lst, "w", encoding="utf-8") as f:
                        f.write("\n".join(names))
                    r = subprocess.run([tc, "-nologo", "-y", "-f", "BC1_UNORM", "-m", "4", "-if", "BOX", "-o", os.path.join(root, folder), "-flist", lst], capture_output=True, text=True)
                    if r.returncode != 0:
                        sys.exit("texconv failed on tiles: " + r.stdout[-400:] + r.stderr[-400:])
                    for p in names:
                        os.remove(p)
                    written += len(names)
            print("tiles", edid, "chunk", ci, cj, "written", written, "plain", skipped, round(time.time() - t0), "s", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(root) for f in fs)
    return dict(tiles=written, plain=skipped, mb=round(size / 1e6), seconds=round(time.time() - t0))


def write_slot_files(tdir, tc):
    d = os.path.join(tdir, "slot")
    if os.path.isfile(os.path.join(d, "s%02d.dds" % (SLOTS - 1))):
        return
    os.makedirs(d, exist_ok=True)
    for n in range(SLOTS):
        to_dds(np.tile(EDGE_NIGHT.astype(np.uint8), (4, 4, 1)), os.path.join(d, "s%02d.dds" % n), tc)


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
    use = min(cfg.get("ppc", ppc), MAX_SIDE // max(w, h))
    # both sides must be multiples of four cells' worth of block compression
    t1 = time.time()
    # "sea": true in worlds.json for a land with an open sea; elsewhere the default water level would flood its valleys
    sea = markers_mod.water_levels.get(edid.lower()) if cfg.get("sea", False) else None
    if sea is not None and sea < -1.0e6:
        sea = None
    day, night, stats, timing = render_map.render(wd, rect, use, mk, sea, lanterns_mod.collect(edid), lights_mod.collect(edid) if cfg.get("lights") else ())
    t2 = time.time()
    if cfg.get("retouch"):
        day = retouch.apply(day, rect, use, cfg["retouch"])
        night = retouch.apply(night, rect, use, cfg["retouch"])
    edge = use * cfg.get("edge_cells", 1.5)
    day = fade_edges(day.copy(), edge, EDGE_DAY)
    night = fade_edges(night.copy(), edge, EDGE_NIGHT)
    low = edid.lower()
    tdir = os.path.join(out, "textures", "terrain", "worldmaprender")
    if preview:
        os.makedirs(preview, exist_ok=True)
        for tag, img in (("day", day), ("night", night)):
            im = Image.fromarray(img)
            scale = max(1, max(im.size) // 2048)
            im.reduce(scale).save(os.path.join(preview, "%s-%s.png" % (low, tag)))
        if cfg.get("single"):
            # the picture the game won't use, full size, so the two can be compared
            other = "day" if cfg["single"] == "night" else "night"
            Image.fromarray(day if other == "day" else night).save(os.path.join(preview, "%s-%s-full.png" % (low, other)))
    # "single": "night" or "day": a land with no sky cycle has one picture, and the plugin never swaps it
    single = cfg.get("single")
    to_dds(night if single == "night" else day, os.path.join(tdir, low + ".dds"), tc)
    if not single:
        to_dds(night, os.path.join(tdir, low + "_n.dds"), tc)
    elif os.path.isfile(os.path.join(tdir, low + "_n.dds")):
        os.remove(os.path.join(tdir, low + "_n.dds"))
    t3 = time.time()
    hx, hy = write_mesh(edid, cfg, rect, out)
    return dict(world=edid, sea=sea, rect=list(rect), host=[hx, hy], size=[day.shape[1], day.shape[0]], ppc=use, markers=len(mk), stats={k: v for k, v in stats.items() if k != "missing"}, missing=stats["missing"],
                timing=dict(render=round(t2 - t1, 1), markers=round(t1 - t0, 1), compress=round(t3 - t2, 1)), dds_mb=round(os.path.getsize(os.path.join(tdir, low + ".dds")) / 1e6, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--world", action="append")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ppc", type=int, default=128)
    ap.add_argument("--preview")
    ap.add_argument("--tiles", action="store_true", help="also draw the tiles of lands with tile_ppc")
    ap.add_argument("--tiles-only", action="store_true", help="for the lands named, the tiles and mesh only: the sheet stays as it is")
    ap.add_argument("--tile-window", type=int, nargs=4, help="i0 j0 i1 j1: only these tiles, as a trial")
    ap.add_argument("--table-only", action="store_true", help="draw nothing: rewrite the table and marker list from worlds.json")
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
    for edid in ([] if args.table_only else names):
        if edid not in cfg:
            sys.exit("%s is not in worlds.json" % edid)
        low = edid.lower()
        if args.tiles_only:
            rect = tuple(report[edid]["rect"]) if edid in report else tuple(cfg[edid]["rect"])
            write_slot_files(tdir, tc)
            write_mesh(edid, cfg[edid], rect, args.out)
            report.setdefault(edid, {})["tile_run"] = build_tiles(edid, cfg[edid], rect, args.out, tc, args.tile_window)
            print(report[edid]["tile_run"], flush=True)
            json.dump(report, open(report_path, "w", encoding="utf-8"), indent=1)
            continue
        if cfg[edid].get("keep") and edid not in (args.world or []) and os.path.isfile(os.path.join(tdir, low + ".dds")):
            print("approved, kept", edid)
            continue
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
        if args.tiles and cfg[edid].get("tile_ppc"):
            write_slot_files(tdir, tc)
            report[edid]["tile_run"] = build_tiles(edid, cfg[edid], tuple(report[edid]["rect"]), args.out, tc, args.tile_window)
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
    places = ["# World Map Render tiles: edid x0 y1 (cells)"]
    for edid, c in cfg.items():
        if os.path.isdir(os.path.join(tdir, "t", edid.lower())):
            r = c["rect"] if c["rect"] != "auto" else report[edid]["rect"]
            places.append("%s %d %d" % (edid, r[0], r[3]))
    with open(os.path.join(args.out, "SKSE", "Plugins", "WorldMapRenderTiles.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(places) + "\n")
    print("table lands:", len(lines) - 3, "tile lands:", len(places) - 1)


if __name__ == "__main__":
    main()
