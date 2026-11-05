"""World Map Render: orthographic top-down pictures of a worldspace, from the list's own LOD files.

Reads xLODGen Output (terrain and water, WIN season), DynDOLOD Output (objects, trees, atlases) and draws them straight
down on the GPU: x and y are world coordinates, z is only the depth test. Two pictures come out of one pass, day and
night. Nothing is written into the modlist.

    render_map.py --world Tamriel --rect -16 -28 32 20 --ppc 64 --out-day day.png --out-night night.png

--rect is x0 y0 x1 y1 in cells; --ppc is pixels per cell (a cell is 4096 units). --raw writes .npy for dds conversion.
"""
import argparse
import glob
import io
import json
import os
import re
import sys
import time

import moderngl
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bsa as bsa_mod  # noqa: E402
import markers as markers_mod  # noqa: E402
import nifread  # noqa: E402

CELL = 4096.0
ZMIN, ZMAX = -60000.0, 120000.0
MODS = r"D:\Modlists\Njordlinger\mods"
PROFILE = r"D:\Modlists\Njordlinger\profiles\Njordlinger\modlist.txt"
CACHE = r"C:\Builds\worldmap-render\work\texture-paths.json"

VS = """
#version 330
uniform vec4 rect;      // x0 y0 x1 y1 in world units
uniform vec3 origin;
in vec3 in_pos;
in vec2 in_uv;
in vec4 in_col;
out vec3 vW;
out vec2 vUV;
out vec4 vC;
void main() {
    vec3 w = in_pos + origin;
    vW = w; vUV = in_uv; vC = in_col;
    float depth = 1.0 - 2.0 * (w.z - (%f)) / (%f);
    gl_Position = vec4(2.0 * (w.x - rect.x) / (rect.z - rect.x) - 1.0,
                       2.0 * (w.y - rect.y) / (rect.w - rect.y) - 1.0, depth, 1.0);
}
""" % (ZMIN, ZMAX - ZMIN)

LIGHT = """
uniform int night;
const vec3 SNOW = vec3(0.86, 0.89, 0.93);
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
}
// Permanent winter: anything facing up lies under snow, whatever its LOD colour; steep faces keep their own.
float snowcover(vec3 n, vec2 w, float lo, float hi) {
    float k = noise(w / 170.0) * 0.5 + noise(w / 900.0) * 0.5;
    return smoothstep(lo, hi, n.z + (k - 0.5) * 0.28);
}
vec3 light(vec3 albedo, vec3 n, float glow) {
    vec3 L = normalize(vec3(-0.55, 0.60, 0.58));
    float d = max(dot(n, L), 0.0);
    if (night == 1) {
        vec3 c = albedo * (vec3(0.20, 0.25, 0.38) + vec3(0.26, 0.31, 0.44) * d);
        return c + glow * vec3(1.0, 0.72, 0.42);
    }
    return albedo * (vec3(0.56, 0.62, 0.74) + vec3(0.62, 0.58, 0.50) * d);
}
"""

FS_LAND = """
#version 330
uniform sampler2D diffuse;
uniform sampler2D normalmap;
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
""" + LIGHT + """
void main() {
    vec3 a = texture(diffuse, vUV).rgb;
    vec3 t = texture(normalmap, vUV).rgb * 2.0 - 1.0;
    vec3 n = normalize(vec3(t.r, t.b, t.g));  // the LOD normal maps store X, Z, Y
    n = normalize(n);
    // LOD ground the winter swap missed (grass, dirt): warm colours go under the snow, grey roads and rock stay.
    float warm = (a.r + a.g) * 0.5 - a.b;
    float cover = smoothstep(0.012, 0.075, warm) * snowcover(n, vW.xy, 0.55, 0.80);
    a = mix(a, SNOW * 0.97, cover);
    n.xy *= 1.6;                              // the height shows better on a flat picture
    f = vec4(light(a, normalize(n), 0.0), 1.0);
}
"""

FS_WATER = """
#version 330
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
uniform int night;
void main() {
    vec3 c = night == 1 ? vec3(0.035, 0.055, 0.095) : vec3(0.27, 0.38, 0.47);
    f = vec4(c, 0.86);
}
"""

FS_OBJ = """
#version 330
uniform sampler2D diffuse;
uniform int alphatest;
in vec3 vW; in vec2 vUV; in vec4 vC;
out vec4 f;
""" + LIGHT + """
void main() {
    vec4 t = texture(diffuse, vUV);
    if (alphatest == 1 && t.a < 0.45) discard;
    vec3 n = normalize(cross(dFdx(vW), dFdy(vW)));
    if (n.z < 0.0) n = -n;
    vec3 a = t.rgb * vC.rgb;
    a = mix(a, SNOW, snowcover(n, vW.xy, 0.62, 0.86) * 0.96);
    f = vec4(light(a, n, 0.0), 1.0);
}
"""

# A tree seen from above is a soft dome in the billboard's own colour.
VS_TREE = """
#version 330
uniform vec4 rect;
in vec2 in_corner;
in vec4 in_inst;     // x y z radius
in vec3 in_rgb;
out vec2 vP; out vec3 vRGB; out float vZ; out float vR;
void main() {
    vP = in_corner; vRGB = in_rgb; vZ = in_inst.z; vR = in_inst.w;
    vec2 w = in_inst.xy + in_corner * in_inst.w;
    float depth = 1.0 - 2.0 * (in_inst.z - (%f)) / (%f);
    gl_Position = vec4(2.0 * (w.x - rect.x) / (rect.z - rect.x) - 1.0,
                       2.0 * (w.y - rect.y) / (rect.w - rect.y) - 1.0, depth, 1.0);
}
""" % (ZMIN, ZMAX - ZMIN)

FS_TREE = """
#version 330
in vec2 vP; in vec3 vRGB; in float vZ; in float vR;
out vec4 f;
""" + LIGHT + """
void main() {
    float r = dot(vP, vP);
    if (r > 1.0) discard;
    vec3 n = normalize(vec3(vP * 0.9, 0.9));
    // the star of a conifer's boughs, seen from above; the dark comes from the billboard's own colour
    float ang = atan(vP.y, vP.x);
    float bough = 0.55 + 0.45 * sin(ang * 7.0 + vR);
    if (r > 0.45 + 0.55 * bough) discard;
    vec3 c = vRGB * vec3(0.42, 0.50, 0.46);
    f = vec4(light(mix(c, SNOW, 0.34 * (1.0 - r)), n, 0.0), 1.0);
}
"""

# Where the LOD has lit windows, a warm pool of light at night.
FS_GLOW = """
#version 330
in vec2 vP; in vec3 vRGB;
out vec4 f;
void main() {
    float r = dot(vP, vP);
    if (r > 1.0) discard;
    float a = (1.0 - r);
    f = vec4(vec3(1.0, 0.68, 0.36) * a * a * vRGB.x, 1.0);
}
"""


def active_mods():
    out = []
    for line in open(PROFILE, encoding="utf-8", errors="replace"):
        line = line.rstrip("\r\n")
        if line.startswith("+"):
            out.append(line[1:])
    return out


class Textures:
    """Finds the winning loose file for a texture path, highest priority mod first; remembers the answer."""

    def __init__(self):
        self.mods = active_mods()
        self.cache = {}
        if os.path.isfile(CACHE):
            self.cache = json.load(open(CACHE, encoding="utf-8"))
        self.dirty = False

    def find(self, path):
        rel = re.sub(r"^data[\\/]", "", path.strip().lower().replace("/", "\\"))
        if rel in self.cache:
            return self.cache[rel]
        hit = None
        for m in self.mods:
            p = os.path.join(MODS, m, rel)
            if os.path.isfile(p):
                hit = p
                break
        self.cache[rel] = hit
        self.dirty = True
        return hit

    def archived(self, path):
        """Bytes of a texture only the archives hold; None when they do not have it either."""
        if not hasattr(self, "library"):
            self.library = bsa_mod.Library(MODS, self.mods, markers_mod.STOCK, markers_mod.active_plugins())
        rel = re.sub(r"^data[\\/]", "", path.strip().lower().replace("/", "\\"))
        a = self.library.find(rel)
        return a.read(rel) if a else None

    def save(self):
        if self.dirty:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            json.dump(self.cache, open(CACHE, "w", encoding="utf-8"))
            self.dirty = False


SRGB_TO_UNORM = {72: 71, 75: 74, 78: 77, 99: 98, 29: 28, 91: 87, 93: 88}


def open_dds(path):
    """Pillow reads the linear DXGI formats only; the bits are the same, so relabel the sRGB ones."""
    d = bytearray(open(path, "rb").read())
    if d[84:88] == b"DX10":
        f = int.from_bytes(d[128:132], "little")
        if f in SRGB_TO_UNORM:
            d[128:132] = SRGB_TO_UNORM[f].to_bytes(4, "little")
    im = Image.open(io.BytesIO(bytes(d)))
    im.load()
    return im


def open_dds_bytes(raw):
    d = bytearray(raw)
    if d[84:88] == b"DX10":
        f = int.from_bytes(d[128:132], "little")
        if f in SRGB_TO_UNORM:
            d[128:132] = SRGB_TO_UNORM[f].to_bytes(4, "little")
    im = Image.open(io.BytesIO(bytes(d)))
    im.load()
    return im


def load_texture(ctx, path, mips=True, limit=0, raw=None):
    im = open_dds_bytes(raw) if raw else open_dds(path)
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    if limit and max(im.size) > limit:
        f = max(im.size) // limit
        im = im.reduce(max(f, 1))
    tex = ctx.texture(im.size, 4, im.tobytes())
    tex.repeat_x = tex.repeat_y = True
    if mips:
        tex.build_mipmaps()
        tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        tex.anisotropy = 8.0
    return tex


def tree_colours(atlas_path, lst):
    """Mean colour of each billboard's opaque pixels."""
    im = open_dds(atlas_path)
    a = np.asarray(im.convert("RGBA"), dtype=np.float32) / 255.0
    H, W = a.shape[:2]
    out = {}
    for i, (w, h, u0, v0, u1, v1) in lst.items():
        c = a[int(v0 * H):max(int(v1 * H), int(v0 * H) + 1), int(u0 * W):max(int(u1 * W), int(u0 * W) + 1)]
        m = c[..., 3] > 0.5
        out[i] = c[m][:, :3].mean(0) if m.any() else np.array([0.2, 0.25, 0.2])
    return out


def world_tiles(world, rect, lod=4):
    """Level 4 tiles of this land near the rect, from the terrain files on disk; one tile of margin for overhanging objects."""
    x0, y0, x1, y1 = rect
    d = os.path.join(MODS, "xLODGen Output", "meshes", "terrain", world.folder)
    out = set()
    for f in glob.glob(os.path.join(d, "*.4.*.btr")):
        m = re.match(r".*\.4\.(-?\d+)\.(-?\d+)(\.win)?\.btr$", f, re.I)
        if m:
            tx, ty = int(m.group(1)), int(m.group(2))
            if x0 - 2 * lod <= tx <= x1 + lod and y0 - 2 * lod <= ty <= y1 + lod:
                out.add((tx, ty))
    return sorted(out)


class Scene:
    def __init__(self, rect_cells, ppc, world):
        self.rect_cells = rect_cells
        self.world = world
        self.size = (int(round((rect_cells[2] - rect_cells[0]) * ppc)), int(round((rect_cells[3] - rect_cells[1]) * ppc)))
        self.ctx = moderngl.create_standalone_context()
        c = self.ctx
        self.fbs = []
        for _ in range(2):
            col = c.texture(self.size, 4)
            dep = c.depth_renderbuffer(self.size)
            self.fbs.append((c.framebuffer([col], dep), col))
        self.land = c.program(vertex_shader=VS, fragment_shader=FS_LAND)
        self.water = c.program(vertex_shader=VS, fragment_shader=FS_WATER)
        self.obj = c.program(vertex_shader=VS, fragment_shader=FS_OBJ)
        self.tree = c.program(vertex_shader=VS_TREE, fragment_shader=FS_TREE)
        self.glow = c.program(vertex_shader=VS_TREE, fragment_shader=FS_GLOW)
        r = tuple(v * CELL for v in rect_cells)
        for p in (self.land, self.water, self.obj, self.tree, self.glow):
            p["rect"].value = r
        self.atlases = {}
        self.stats = dict(tiles=0, land_tris=0, water_tris=0, obj_tris=0, trees=0, glows=0, missing=set())
        self.tex = Textures()

    def clear(self):
        for i, (fb, _) in enumerate(self.fbs):
            fb.use()
            fb.clear(0.13, 0.16, 0.2, 1.0, depth=1.0)

    def draw_both(self, prog, vao, setup=None):
        for i, (fb, _) in enumerate(self.fbs):
            fb.use()
            if "night" in prog:
                prog["night"].value = i
            vao.render(moderngl.TRIANGLES)

    def _mesh(self, prog, verts, uvs, cols, tris, origin):
        c = self.ctx
        n = len(verts)
        data = np.zeros((n, 9), dtype="f4")
        data[:, :3] = verts
        if uvs is not None:
            data[:, 3:5] = uvs
        if cols is not None:
            data[:, 5:9] = cols[:, :4] / 255.0
        else:
            data[:, 5:9] = 1.0
        vb = c.buffer(data.tobytes())
        ib = c.buffer(tris.astype("u4").tobytes())
        wanted = [("in_pos", "3f"), ("in_uv", "2f"), ("in_col", "4f")]
        fmt = []
        names = []
        for name, f in wanted:
            if name in prog:
                fmt.append(f)
                names.append(name)
            else:
                fmt.append("%dx4" % int(f[0]))
        vao = c.vertex_array(prog, [(vb, " ".join(fmt), *names)], ib)
        prog["origin"].value = origin
        return vao, vb, ib

    def draw_terrain(self, tiles):
        c = self.ctx
        wd = self.world
        for (tx, ty) in tiles:
            base = os.path.join(MODS, "xLODGen Output", "meshes", "terrain", wd.folder, "%s.4.%d.%d" % (wd.lower, tx, ty))
            p = base + ".WIN.btr"
            if not os.path.isfile(p):
                p = base + ".btr"
            if not os.path.isfile(p):
                continue
            tdir = os.path.join(MODS, "xLODGen Output", "textures", "terrain", wd.lower)
            dp = os.path.join(tdir, "%s.4.%d.%d.win.dds" % (wd.lower, tx, ty))
            if not os.path.isfile(dp):
                dp = os.path.join(tdir, "%s.4.%d.%d.dds" % (wd.lower, tx, ty))
            np_ = os.path.join(tdir, "%s.4.%d.%d.win_n.dds" % (wd.lower, tx, ty))
            if not os.path.isfile(np_):
                np_ = os.path.join(tdir, "%s.4.%d.%d_n.dds" % (wd.lower, tx, ty))
            shapes = nifread.parse(p)
            origin = (tx * CELL, ty * CELL, 0.0)
            self.stats["tiles"] += 1
            texs = None
            if os.path.isfile(dp) and os.path.isfile(np_):
                texs = (load_texture(c, dp), load_texture(c, np_))
            for s in shapes:
                if s.name.lower() != "land" or texs is None:
                    continue
                vao, vb, ib = self._mesh(self.land, s.verts, s.uvs, None, s.tris, origin)
                texs[0].use(0)
                texs[1].use(1)
                self.land["diffuse"].value = 0
                self.land["normalmap"].value = 1
                c.enable(moderngl.DEPTH_TEST)
                self.draw_both(self.land, vao)
                vao.release(); vb.release(); ib.release()
                self.stats["land_tris"] += len(s.tris)
            if texs:
                texs[0].release(); texs[1].release()

    def draw_water(self, tiles):
        c = self.ctx
        wd = self.world
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        for (tx, ty) in tiles:
            base = os.path.join(MODS, "xLODGen Output", "meshes", "terrain", wd.folder, "%s.4.%d.%d" % (wd.lower, tx, ty))
            p = base + ".btr"
            if not os.path.isfile(p):
                p = base + ".WIN.btr"
            if not os.path.isfile(p):
                continue
            origin = (tx * CELL, ty * CELL, 0.0)
            for sh in nifread.parse(p):
                if sh.name.lower() == "land" or sh.tris is None:
                    continue
                vao, vb, ib = self._mesh(self.water, sh.verts, None, None, sh.tris, origin)
                self.draw_both(self.water, vao)
                vao.release(); vb.release(); ib.release()
                self.stats["water_tris"] += len(sh.tris)
        c.disable(moderngl.BLEND)

    def atlas(self, path):
        key = path.lower()
        if key not in self.atlases:
            f = self.tex.find(path)
            big = "dyndolod\\lod" in key
            try:
                self.atlases[key] = load_texture(self.ctx, f, limit=0 if big else 512) if f else None
            except Exception as e:
                print("cannot read", f, e)
                self.atlases[key] = None
                f = None
            if not f:
                raw = None
                try:
                    raw = self.tex.archived(path)
                    if raw:
                        self.atlases[key] = load_texture(self.ctx, None, limit=512, raw=raw)
                except Exception as e:
                    print("cannot read archived", key, e)
                    raw = None
                if not raw:
                    # not even in the archives: a plain stone colour, which the snow then covers
                    self.stats["missing"].add(key)
                    self.atlases[key] = self.plain()
        return self.atlases[key]

    def plain(self):
        if not hasattr(self, "_plain"):
            self._plain = self.ctx.texture((1, 1), 4, bytes([96, 94, 90, 255]))
        return self._plain

    def draw_objects(self, tiles):
        c = self.ctx
        wd = self.world
        c.enable(moderngl.DEPTH_TEST)
        glow = []
        d = os.path.join(MODS, "DynDOLOD Output", "meshes", "Terrain", wd.dyn, "Objects")
        for (tx, ty) in tiles:
            p = os.path.join(d, "%s.4.%d.%d.bto" % (wd.dyn, tx, ty))
            if not os.path.isfile(p):
                continue
            for s in nifread.parse(p):
                if s.tris is None or s.uvs is None or not s.textures or s.name.lower() == "treepassthru":
                    continue
                t = s.textures[0].lower()
                if "_glow" in t:
                    centres = s.verts[s.tris].mean(1)
                    glow.append(centres)
                tex = self.atlas(s.textures[0])
                if tex is None:
                    continue
                vao, vb, ib = self._mesh(self.obj, s.verts, s.uvs, s.colors, s.tris, (0.0, 0.0, 0.0))
                tex.use(0)
                self.obj["diffuse"].value = 0
                self.obj["alphatest"].value = 1 if "alpha" in t else 0
                self.draw_both(self.obj, vao)
                vao.release(); vb.release(); ib.release()
                self.stats["obj_tris"] += len(s.tris)
        return np.concatenate(glow) if glow else np.zeros((0, 3), "f4")

    def _sprites(self, prog, inst, rgb, use):
        c = self.ctx
        if not len(inst):
            return
        quad = c.buffer(np.array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1], "f4").tobytes())
        ib = c.buffer(inst.astype("f4").tobytes())
        cb = c.buffer(rgb.astype("f4").tobytes())
        content = [(quad, "2f", "in_corner"), (ib, "4f/i", "in_inst")]
        if "in_rgb" in prog:
            content.append((cb, "3f/i", "in_rgb"))
        vao = c.vertex_array(prog, content)
        for i in use:
            self.fbs[i][0].use()
            if "night" in prog:
                prog["night"].value = i
            vao.render(moderngl.TRIANGLES, instances=len(inst))
        vao.release(); quad.release(); ib.release(); cb.release()

    def draw_trees(self, tiles):
        wd = self.world
        d = os.path.join(MODS, "DynDOLOD Output", "meshes", "Terrain", wd.dyn, "Trees")
        lp = os.path.join(d, wd.dyn + ".lst")
        ap = os.path.join(MODS, "DynDOLOD Output", "textures", "Terrain", wd.dyn, "Trees", wd.dyn + "TreeLOD.dds")
        if not (os.path.isfile(lp) and os.path.isfile(ap)):
            return
        lst = nifread.read_lst(lp)
        cols = tree_colours(ap, lst)
        insts, rgbs = [], []
        for (tx, ty) in tiles:
            p = os.path.join(d, "%s.4.%d.%d.btt" % (wd.dyn, tx, ty))
            if not os.path.isfile(p):
                continue
            for t, a in nifread.read_btt(p).items():
                if t not in lst or not len(a):
                    continue
                w, h = lst[t][:2]
                n = len(a)
                i = np.zeros((n, 4), "f4")
                i[:, 0:2] = a[:, 0:2]
                i[:, 2] = a[:, 2] + 0.7 * h * a[:, 4]
                i[:, 3] = np.minimum(0.24 * w * a[:, 4], 380.0)
                insts.append(i)
                rgbs.append(np.tile(cols[t], (n, 1)))
        if insts:
            insts, rgbs = np.concatenate(insts), np.concatenate(rgbs)
            self.stats["trees"] = len(insts)
            self.ctx.enable(moderngl.DEPTH_TEST)
            self._sprites(self.tree, insts, rgbs, (0, 1))

    def draw_glow(self, centres, markers):
        """Lit windows in the LOD, weakly, and a pool at each city, town and settlement the markers name."""
        # the marker's icon type: castles and town halls, towns, then lit single buildings (shops, inns, smiths, houses, temples)
        sizes = {1: (2600.0, 0.6), 2: (1500.0, 0.55), 3: (800.0, 0.4), 0x0F: (700.0, 0.3), 0x1E: (700.0, 0.3), 0x22: (700.0, 0.3),
                 0x23: (2200.0, 0.5), 0x25: (2200.0, 0.5), 0x28: (2200.0, 0.5), 0x2C: (2200.0, 0.5)}
        pools = [(m["x"], m["y"], sizes[m["type"]][0], sizes[m["type"]][1]) for m in markers if m.get("type") in sizes]
        if len(centres):
            pools.extend((c[0], c[1], 640.0, 0.05) for c in centres)
        if not pools:
            return
        self.stats["glows"] = len(pools)
        c = self.ctx
        c.disable(moderngl.DEPTH_TEST)
        c.enable(moderngl.BLEND)
        c.blend_func = (moderngl.ONE, moderngl.ONE)
        a = np.array(pools, "f4")
        inst = np.zeros((len(pools), 4), "f4")
        inst[:, :2] = a[:, :2]
        inst[:, 3] = a[:, 2]
        rgb = np.zeros((len(pools), 3), "f4")
        rgb[:, 0] = a[:, 3]
        self._sprites(self.glow, inst, rgb, (1,))
        c.disable(moderngl.BLEND)
        c.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)

    def read(self, i):
        fb, col = self.fbs[i]
        raw = col.read()
        a = np.frombuffer(raw, dtype=np.uint8).reshape(self.size[1], self.size[0], 4)[::-1, :, :3]
        return a


class World:
    def __init__(self, editor_id, dyn=None, folder=None):
        self.id = editor_id
        self.lower = editor_id.lower()
        self.folder = folder or editor_id
        self.dyn = dyn or editor_id


def render(world, rect, ppc, markers=()):
    t0 = time.time()
    sc = Scene(rect, ppc, world)
    sc.clear()
    tiles = world_tiles(world, rect)
    if not tiles:
        raise SystemExit("no level 4 terrain for %s in rect %s" % (world.id, rect))
    t1 = time.time()
    sc.draw_terrain(tiles)
    t2 = time.time()
    sc.draw_water(tiles)
    t3 = time.time()
    centres = sc.draw_objects(tiles)
    t4 = time.time()
    sc.draw_trees(tiles)
    t5 = time.time()
    sc.draw_glow(centres, markers)
    sc.tex.save()
    day, night = sc.read(0), sc.read(1)
    t6 = time.time()
    sc.stats["missing"] = sorted(sc.stats["missing"])
    timing = dict(setup=t1 - t0, terrain=t2 - t1, water=t3 - t2, objects=t4 - t3, trees=t5 - t4, finish=t6 - t5, total=t6 - t0)
    return day, night, sc.stats, timing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="Tamriel")
    ap.add_argument("--rect", type=float, nargs=4, required=True)
    ap.add_argument("--ppc", type=float, default=64)
    ap.add_argument("--dyn", default=None)
    ap.add_argument("--folder", default=None)
    ap.add_argument("--out-day")
    ap.add_argument("--out-night")
    ap.add_argument("--stats")
    ap.add_argument("--markers")
    args = ap.parse_args()
    w = World(args.world, args.dyn, args.folder)
    mk = json.load(open(args.markers, encoding="utf-8")) if args.markers else ()
    day, night, stats, timing = render(w, tuple(args.rect), args.ppc, mk)
    if args.out_day:
        Image.fromarray(day).save(args.out_day)
    if args.out_night:
        Image.fromarray(night).save(args.out_night)
    out = dict(stats=stats, timing={k: round(v, 2) for k, v in timing.items()}, size=[day.shape[1], day.shape[0]])
    if args.stats:
        json.dump(out, open(args.stats, "w"), indent=1)
    print(json.dumps(out)[:1500])


if __name__ == "__main__":
    main()
