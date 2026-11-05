"""Sweep a land's picture for right angles: long straight horizontal or vertical edges, and stair steps.

A straight line is fine where a building or wall really stands, by the land's own references. Anywhere else it is a defect of the
render: a land with such hits does not stage unless worlds.json carries a sweep_note saying why. build_maps.py and retouch.py call this
before they write a picture, so it can't be skipped.

    right_angles.py <data folder>                 every land, every picture: a report, an overview for each, exit 1 if a land fails
    right_angles.py <data folder> --world EDID    one land
"""
import argparse, json, os, re, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

Image.MAX_IMAGE_PIXELS = None
HERE = os.path.dirname(os.path.abspath(__file__))
CELL = 4096.0
NOT_BUILT = re.compile(r"mountain|rock|cliff|boulder|ice|glacier|snowdrift|landscape\\(?!bridges)|tree|plant|grass|fx|dummy|marker", re.I)
BIG = re.compile(r"architecture|castle|fort|dwemer|dwarven|wall|bridge|ship|dock|tower|temple|nordic", re.I)


def structures(world, rect, ppc):
    """Where real buildings and walls stand: a mask at an eighth of the picture's size, from the land's references."""
    import land_direct
    s = 8
    w, h = int((rect[2] - rect[0]) * ppc) // s, int((rect[3] - rect[1]) * ppc) // s
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    for rel, refs in land_direct.placed_models(world, False).items():
        name = rel.split("|")[0]
        if NOT_BUILT.search(name):
            continue
        r = (1800.0 if BIG.search(name) else 800.0) / CELL * ppc / s
        for ref in refs:
            x, y = (ref[0] / CELL - rect[0]) * ppc / s, (rect[3] - ref[1] / CELL) * ppc / s
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return np.asarray(m) > 0, s


def find_runs(rgb, ppc, skip):
    """Runs of a sharp step along a row or a column, at least a quarter of a cell long, inside the part the camera can reach."""
    lum = rgb.astype(np.float32).mean(2)
    min_run = max(8, int(ppc / 4))
    runs = []
    for axis in (0, 1):
        m = lum if axis == 0 else lum.T
        step = np.abs(m[2:, :] - m[:-2, :])
        hit = step > np.maximum(10.0, 0.12 * (m[2:, :] + m[:-2, :]) / 2)
        for i in np.where(hit.sum(1) >= min_run)[0]:
            if i < skip or i >= hit.shape[0] - skip:
                continue
            d = np.diff(np.concatenate([[0], hit[i].astype(np.int8), [0]]))
            for a, b in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
                a, b = max(int(a), skip), min(int(b), hit.shape[1] - skip)
                if b - a >= min_run:
                    runs.append((axis, int(i) + 1, a, b))
    return runs


def stairs(rgb, mask, s):
    """Corners of little steps per million pixels, away from buildings: an edge turning square every few pixels."""
    lum = rgb.astype(np.float32).mean(2)
    gx = np.abs(lum[:, 1:] - lum[:, :-1])[:-1, :] > 22
    gy = np.abs(lum[1:, :] - lum[:-1, :])[:, :-1] > 22
    # one direction here, the other in the next pixel along: a square corner
    corner = (gx[:-3, :-3] & gx[3:, :-3] & gy[:-3, :-3] & gy[:-3, 3:]).astype(np.uint8)
    h, w = corner.shape
    keep = ~np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((w, h))).astype(bool)
    return round(float((corner.astype(bool) & keep).sum()) * 1e6 / (h * w), 1)


def classify(runs, mask, s):
    real, odd = [], []
    h, w = mask.shape
    for r in runs:
        ax, i, a, b = r
        n = 0
        pts = range(a, b, max(1, s))
        for t in pts:
            x, y = (t, i) if ax == 0 else (i, t)
            if mask[min(y // s, h - 1), min(x // s, w - 1)]:
                n += 1
        (real if n >= 0.5 * len(pts) else odd).append(r)
    return real, odd


def heal(img, runs, ppc, grow=1):
    """Take the hardness out of defects: a soft blur across each run, fading away from the line and at its ends. Nothing moves."""
    out = img.copy()
    t = max(4, int(ppc / 20)) * grow
    H, W = out.shape[:2]
    for ax, i, a, b in runs:
        y0, y1 = (max(0, i - 2 * t), min(H, i + 2 * t)) if ax == 0 else (max(0, a - t), min(H, b + t))
        x0, x1 = (max(0, a - t), min(W, b + t)) if ax == 0 else (max(0, i - 2 * t), min(W, i + 2 * t))
        if y1 <= y0 or x1 <= x0:
            continue
        part = out[y0:y1, x0:x1]
        soft = np.asarray(Image.fromarray(part).filter(ImageFilter.GaussianBlur(t / 2.0))).astype(np.float32)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        across = np.abs(yy - i) if ax == 0 else np.abs(xx - i)
        along = (xx if ax == 0 else yy)
        k = np.clip(1.0 - across / (2.0 * t), 0, 1) * np.clip(np.minimum(along - (a - t), (b + t) - along) / float(t), 0, 1)
        k = (k * k * (3 - 2 * k))[..., None]
        out[y0:y1, x0:x1] = (part * (1 - k) + soft * k).astype(np.uint8)
    return out


def sweep(rgb, world, rect, ppc, edge_cells=1.5, mask=None):
    if mask is None:
        mask, s = structures(world, rect, ppc)
    else:
        mask, s = mask
    runs = find_runs(rgb, ppc, int(edge_cells * ppc))
    real, odd = classify(runs, mask, s)
    return dict(real=real, odd=odd, stairs=stairs(rgb, mask, s), mask=(mask, s))


def clean(img, world, rect, ppc, edge_cells=1.5, rounds=6):
    """Heal what isn't a building until nothing is left or it stops helping."""
    mk = structures(world, rect, ppc)
    for n in range(rounds):
        r = sweep(img, world, rect, ppc, edge_cells, mk)
        if not r["odd"]:
            break
        img = heal(img, r["odd"], ppc, 1 + n // 2)   # what survives gets a wider touch
    return img, mk


def where(run, rect, ppc):
    ax, i, a, b = run
    m = (a + b) / 2.0
    x, y = (m, i) if ax == 0 else (i, m)
    return "%.1f, %.1f (%s, %.1f cells)" % (rect[0] + x / ppc, rect[3] - y / ppc, "across" if ax == 0 else "down", (b - a) / ppc)


def gate(day, night, edid, cfg, rect, ppc):
    """Refuses a land with defects and no note. Returns the counts for the build report."""
    mk = structures(edid, rect, ppc)
    out = {}
    bad = []
    for tag, img in (("day", day), ("night", night)):
        if img is None:
            continue
        r = sweep(img, edid, rect, ppc, cfg.get("edge_cells", 1.5), mk)
        out[tag] = dict(real=len(r["real"]), odd=len(r["odd"]), stairs=r["stairs"])
        bad += [where(x, rect, ppc) for x in r["odd"]]
    if bad and not cfg.get("sweep_note"):
        raise SystemExit("right angles outside buildings, and no sweep_note: " + "; ".join(bad[:12]))
    return out


def decode(path, work, tc):
    os.makedirs(work, exist_ok=True)
    subprocess.run([tc, "-nologo", "-y", "-m", "1", "-f", "R8G8B8A8_UNORM", "-ft", "png", "-o", work, path], capture_output=True, check=True)
    return np.asarray(Image.open(os.path.join(work, os.path.basename(path)[:-4] + ".png")).convert("RGB"))


def overview(img, r, out):
    o = Image.fromarray(img)
    d = ImageDraw.Draw(o)
    lw = max(3, img.shape[1] // 800)
    for rows, col in ((r["real"], (0, 200, 255)), (r["odd"], (255, 0, 255))):
        for ax, i, a, b in rows:
            d.line([a, i, b, i] if ax == 0 else [i, a, i, b], fill=col, width=lw)
    o.thumbnail((1800, 1800))
    o.convert("RGB").save(out, quality=88)


def main():
    import build_maps
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--world", action="append")
    ap.add_argument("--label", default="now")
    a = ap.parse_args()
    cfg = {k: v for k, v in json.load(open(os.path.join(HERE, "worlds.json"), encoding="utf-8")).items() if not k.startswith("_")}
    root = os.path.join(a.data, "..")
    rep = json.load(open(os.path.join(root, "build-report.json"), encoding="utf-8"))
    tdir = os.path.join(a.data, "textures", "terrain", "worldmaprender")
    work = os.path.join(root, "sweep", "dec")
    tc = build_maps.texconv()
    result, failed = {}, []
    for edid in (a.world or list(cfg)):
        low = edid.lower()
        rect, ppc = rep[edid]["rect"], rep[edid]["ppc"]
        mk = structures(edid, rect, ppc)
        result[edid] = {}
        for name, tag in ((low, cfg[edid].get("single") or "day"), (low + "_n", "night")):
            src = os.path.join(tdir, name + ".dds")
            if not os.path.isfile(src):
                continue
            img = decode(src, work, tc)
            r = sweep(img, edid, rect, ppc, cfg[edid].get("edge_cells", 1.5), mk)
            # A block's edge runs a cell or more. The compression sharpens short model edges back up to the search's quarter cell, so the two are told apart.
            long_runs = [x for x in r["odd"] if (x[3] - x[2]) >= ppc / 2]
            result[edid][tag] = dict(real=len(r["real"]), odd=len(r["odd"]), long=len(long_runs), stairs=r["stairs"], where=[where(x, rect, ppc) for x in long_runs[:40]])
            overview(img, r, os.path.join(root, "sweep", "%s-%s-%s.jpg" % (low, tag, a.label)))
            os.remove(os.path.join(work, name + ".png"))
            print(edid, tag, "buildings", len(r["real"]), "outside buildings", len(r["odd"]), "of them half a cell or longer", len(long_runs), "stairs", r["stairs"], flush=True)
            if long_runs or (r["odd"] and not cfg[edid].get("sweep_note")):
                failed.append(edid + " " + tag)
    p = os.path.join(root, "sweep", "report-%s.json" % a.label)
    json.dump(result, open(p, "w", encoding="utf-8"), indent=1)
    print("FAILED:" if failed else "all clear", failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
