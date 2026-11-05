"""What stands at a place on a land's map: water squares, the LOD tile's land, painted land textures, and placed models.

    whats_here.py <world> x0 y0 x1 y1      (cells)

Prints each model whose placed bounds reach the area, largest first, with its texture.
"""
import collections
import glob
import os
import struct
import sys
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import land_direct as L  # noqa: E402
import markers as M  # noqa: E402
import nifread  # noqa: E402
import render_map as R  # noqa: E402
import roads  # noqa: E402

CELL = 4096.0


def main():
    world = sys.argv[1]
    x0, y0, x1, y1 = (float(v) for v in sys.argv[2:6])
    X0, Y0, X1, Y1 = x0 * CELL, y0 * CELL, x1 * CELL, y1 * CELL
    print("== LOD tiles")
    for f in glob.glob(os.path.join(R.MODS, "xLODGen Output", "meshes", "terrain", world, "*.4.*.btr")):
        name = os.path.basename(f).split(".")
        if name[-2].lower() == "win":
            continue
        tx, ty = int(name[2]), int(name[3])
        if tx + 4 <= x0 or tx >= x1 or ty + 4 <= y0 or ty >= y1:
            continue
        for s in nifread.parse(f):
            if s.verts is None or not len(s.verts):
                continue
            v = s.verts + np.array([tx * CELL, ty * CELL, 0], "f4")
            if v[:, 0].max() < X0 or v[:, 0].min() > X1 or v[:, 1].max() < Y0 or v[:, 1].min() > Y1:
                continue
            print("  tile %d,%d %-6s verts %5d z %.0f..%.0f x %.0f..%.0f y %.0f..%.0f" % (tx, ty, s.name[:6], len(v), v[:, 2].min(), v[:, 2].max(), v[:, 0].min() / CELL, v[:, 0].max() / CELL, v[:, 1].min() / CELL, v[:, 1].max() / CELL))
    print("== painted land")
    ltex, txst, lands = roads.scan(world)
    use = collections.Counter()
    cells = 0
    for (cx, cy), (raw, comp, full, plugin) in lands.items():
        if cx + 1 <= x0 or cx >= x1 or cy + 1 <= y0 or cy >= y1:
            continue
        cells += 1
        body = zlib.decompress(raw[4:]) if comp else raw

        def form(fid):
            i = fid >> 24
            return (full[i] if i < len(full) else plugin), fid & 0xFFFFFF

        cur = None
        for t, s in M.subs(body):
            if t == b"BTXT" and len(s) >= 8:
                use[form(struct.unpack_from("<I", s)[0])] += 289
            elif t == b"ATXT" and len(s) >= 8:
                cur = form(struct.unpack_from("<I", s)[0])
            elif t == b"VTXT" and cur:
                use[cur] += len(s) // 8
                cur = None
    print("  cells with land:", cells)
    for k, v in use.most_common(10):
        r = ltex.get(k, {})
        print("  %7d %-32s %s" % (v, r.get("ed", "?"), txst.get(r.get("set"), {}).get("tex", "?")))
    print("== placed models reaching the area")
    tex = R.Textures()
    rows = []
    for rel, refs in L.placed_models(world).items():
        near = [r for r in refs if X0 - 6000 <= r[0] <= X1 + 6000 and Y0 - 6000 <= r[1] <= Y1 + 6000]
        if not near:
            continue
        try:
            sh = L.load_model(rel, tex)
        except Exception:
            sh = None
        if not sh:
            rows.append((0, len(near), rel, "UNREADABLE"))
            continue
        hit = 0
        area = 0.0
        for (x, y, z, a, b, c, sc) in near:
            m = L.rotation(a, b, c) * sc
            for v, *_ in sh:
                w = v @ m.T + np.array([x, y, z], "f4")
                if w[:, 0].max() >= X0 and w[:, 0].min() <= X1 and w[:, 1].max() >= Y0 and w[:, 1].min() <= Y1:
                    hit += 1
                    area = max(area, float(w[:, 0].max() - w[:, 0].min()))
                    break
        if hit:
            rows.append((round(area), hit, rel, sh[0][4][-44:]))
    for r in sorted(rows, reverse=True)[:30]:
        print("  across %6d  x%-4d %-58s %s" % (r[0], r[1], r[2][-58:], r[3]))


if __name__ == "__main__":
    main()
