"""Cosmetic work on a land's picture, outside what the player can reach. Nothing here touches the land's middle.

apply: everything outside hand-drawn outlines, or inside the ones marked remove, becomes smooth ground of the land's own colour.

As a tool, on a picture already drawn:  retouch.py <stage data folder> <edid>
"""
import json, os, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

Image.MAX_IMAGE_PIXELS = None
HERE = os.path.dirname(os.path.abspath(__file__))


def _noise(h, w, seed):
    """Soft blotches, about two cells across, -1 to 1."""
    r = np.random.RandomState(seed)
    a = r.rand(max(2, h // 96), max(2, w // 96)).astype(np.float32)
    b = r.rand(max(2, h // 32), max(2, w // 32)).astype(np.float32)
    def up(x):
        return np.asarray(Image.fromarray((x * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC), dtype=np.float32) / 255.0
    return (up(a) * 0.7 + up(b) * 0.3 - 0.5) * 2.0


def apply(img, rect, ppc, cfg):
    """cfg: keep = outlines in cells (none: the whole picture), remove = outlines to clear, feather = cells,
    ground = [r, g, b] day then night by pixel brightness is not guessed: the land's brightest ground is sampled, or ground_at = a cell point to sample instead."""
    if isinstance(cfg, list):  # several passes, each with its own ground
        for one in cfg:
            img = apply(img, rect, ppc, one)
        return img
    h, w = img.shape[:2]
    scale = 4
    mask = Image.new("L", (w // scale, h // scale), 0 if cfg.get("keep") else 255)
    d = ImageDraw.Draw(mask)
    for poly in cfg.get("keep", []):
        d.polygon([((x - rect[0]) * ppc / scale, (rect[3] - y) * ppc / scale) for x, y in poly], fill=255)
    for poly in cfg.get("remove", []):
        d.polygon([((x - rect[0]) * ppc / scale, (rect[3] - y) * ppc / scale) for x, y in poly], fill=0)
    feather = cfg.get("feather", 0.4) * ppc / scale
    soft = mask.filter(ImageFilter.GaussianBlur(feather))
    keep = np.asarray(soft.resize((w, h), Image.BILINEAR), dtype=np.float32) / 255.0
    inside = np.asarray(mask.resize((w, h), Image.NEAREST)) > 0
    px = img[inside][::97].astype(np.float32)
    lum = px.mean(1)
    ground = np.median(px[lum >= np.percentile(lum, cfg.get("ground_percentile", 70))], axis=0)
    if cfg.get("ground_at"):
        gx, gy = int((cfg["ground_at"][0] - rect[0]) * ppc), int((rect[3] - cfg["ground_at"][1]) * ppc)
        ground = np.median(img[gy - 8:gy + 8, gx - 8:gx + 8].reshape(-1, 3).astype(np.float32), axis=0)
    n = _noise(h, w, 7)[..., None]
    fill = ground[None, None, :] * (1.0 + cfg.get("mottle", 0.05) * n)
    k = keep[..., None]
    out = np.clip(img.astype(np.float32) * k + fill * (1 - k), 0, 255).astype(np.uint8)
    # replace_colour: a flat colour (a land's empty surround drawn as water) becomes the land's snow, blended in over a short distance
    rc = cfg.get("replace_colour")
    if rc:
        gx, gy = int((rc["at"][0] - rect[0]) * ppc), int((rect[3] - rc["at"][1]) * ppc)
        key = np.median(out[gy - 8:gy + 8, gx - 8:gx + 8].reshape(-1, 3).astype(np.float32), axis=0)
        f = out.astype(np.float32)
        m = (np.abs(f - key).max(2) < rc.get("tol", 14)).astype(np.uint8) * 255
        # only the colour that reaches the picture's edge: a lake of a similar blue inside the land stays
        # the compression speckles a flat colour: close the speckle first, or the fill stops at the first hole
        mi = Image.fromarray(m).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        arr = np.asarray(mi)
        ys, xs = np.where(arr[max(0, gy - 40):gy + 40, max(0, gx - 40):gx + 40] == 255)
        if len(ys):
            ImageDraw.floodfill(mi, (int(xs[0]) + max(0, gx - 40), int(ys[0]) + max(0, gy - 40)), 128)
        m = (np.asarray(mi) == 128).astype(np.uint8) * 255
        soft = np.asarray(Image.fromarray(m).filter(ImageFilter.GaussianBlur(max(2.0, ppc / 24.0))), dtype=np.float32)[..., None] / 255.0
        snow = ground[None, None, :] * (1.0 + 0.05 * _noise(h, w, 11)[..., None])
        out = np.clip(f * (1 - soft) + snow * soft, 0, 255).astype(np.uint8)
    # heal: rectangles x0 y0 x1 y1 in cells whose drawn outline is taken out, each side copied over from just beside it
    for x0, y0, x1, y1 in cfg.get("heal", []):
        px0, px1 = int((x0 - rect[0]) * ppc), int((x1 - rect[0]) * ppc)
        py0, py1 = int((rect[3] - y1) * ppc), int((rect[3] - y0) * ppc)
        t = max(3, ppc // 32)
        for py in (py0, py1):
            out[py - t:py + t, px0 - t:px1 + t] = out[py + t:py + 3 * t, px0 - t:px1 + t]
        for px in (px0, px1):
            out[py0 - t:py1 + t, px - t:px + t] = out[py0 - t:py1 + t, px + t:px + 3 * t]
    return out


def main():
    import build_maps, right_angles
    data, edid = sys.argv[1], sys.argv[2]
    cfg = json.load(open(os.path.join(HERE, "worlds.json"), encoding="utf-8"))[edid]
    report = json.load(open(os.path.join(data, "..", "build-report.json"), encoding="utf-8"))
    rect, ppc = report[edid]["rect"], report[edid]["ppc"]
    tc = build_maps.texconv()
    tdir = os.path.join(data, "textures", "terrain", "worldmaprender")
    work = os.path.join(data, "..", "retouch-work")
    os.makedirs(work, exist_ok=True)
    low = edid.lower()
    # a land with one picture keeps it under the plain name, whichever it is
    first = cfg.get("single") or "day"
    for name, colour, tag in ((low, build_maps.EDGE_NIGHT if first == "night" else build_maps.EDGE_DAY, first), (low + "_n", build_maps.EDGE_NIGHT, "night")):
        src = os.path.join(tdir, name + ".dds")
        if not os.path.isfile(src):
            continue
        subprocess.run([tc, "-nologo", "-y", "-m", "1", "-f", "R8G8B8A8_UNORM", "-ft", "png", "-o", work, src], capture_output=True, check=True)
        img = np.asarray(Image.open(os.path.join(work, name + ".png")).convert("RGB"))
        img = apply(img, rect, ppc, cfg["retouch"])
        img, _ = right_angles.clean(img, edid, rect, ppc, cfg.get("edge_cells", 1.5))
        right_angles.gate(img, None, edid, cfg, rect, ppc)
        img = build_maps.fade_edges(img.copy(), ppc * cfg.get("edge_cells", 1.5), colour)
        build_maps.to_dds(img, src, tc)
        im = Image.fromarray(img)
        im.reduce(max(1, max(im.size) // 2048)).save(os.path.join(data, "..", "preview", "%s-%s.png" % (low, tag)))
        print("retouched", name, img.shape)


if __name__ == "__main__":
    main()
