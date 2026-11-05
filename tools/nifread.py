"""Small reader for the NIF flavours LOD files use (Skyrim SE, 20.2.0.7 / user 12 / bs 100).

Reads what an orthographic map render needs from .btr (terrain LOD), .bto (object LOD) and .btt (tree LOD) files:
every shape's world transform, vertices, UVs, triangles and texture paths. Written from the NIF format
as documented by niftools/nifxml; no game code is used.
"""
import struct
import numpy as np


class Shape:
    __slots__ = ("name", "kind", "verts", "uvs", "colors", "tris", "textures", "alpha", "effect_tex", "sflags", "emissive")

    def __init__(self):
        self.name = ""
        self.kind = ""
        self.verts = None
        self.uvs = None
        self.colors = None
        self.tris = None
        self.textures = []
        self.alpha = None
        self.effect_tex = ""
        self.sflags = (0, 0)
        self.emissive = None


class R:
    def __init__(self, d, p=0):
        self.d, self.p = d, p

    def u8(self):
        v = self.d[self.p]; self.p += 1; return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]; self.p += 2; return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]; self.p += 4; return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]; self.p += 4; return v

    def f(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]; self.p += 4; return v

    def fs(self, n):
        v = struct.unpack_from("<%df" % n, self.d, self.p); self.p += 4 * n; return v

    def sized(self):
        n = self.u32(); s = self.d[self.p:self.p + n]; self.p += n; return s.decode("latin1")

    def short(self):
        n = self.u8(); s = self.d[self.p:self.p + n]; self.p += n; return s.decode("latin1")


def read_header(d):
    nl = d.index(b"\n") + 1
    r = R(d, nl)
    ver = r.u32(); r.u8()
    user = r.u32(); nblocks = r.u32(); bs = r.u32()
    r.short();
    if bs >= 130:
        r.u32()
    r.short()
    if bs < 131:
        r.short()
    ntypes = r.u16()
    types = [r.sized() for _ in range(ntypes)]
    idx = [r.u16() for _ in range(nblocks)]
    sizes = [r.u32() for _ in range(nblocks)]
    nstr = r.u32(); r.u32()
    strs = [r.sized() for _ in range(nstr)]
    ngrp = r.u32(); r.p += 4 * ngrp
    return bs, [types[i] for i in idx], sizes, strs, r.p


def _avobject(r, strs):
    """Name, extra data, controller, flags, transform, collision: returns name, flags, (tx,ty,tz), rot, scale."""
    ni = r.i32(); name = strs[ni] if 0 <= ni < len(strs) else ""
    n = r.u32(); ex = [r.i32() for _ in range(n)]
    r.i32()
    flags = r.u32()
    t = r.fs(3); rot = r.fs(9); s = r.f()
    r.i32()
    return name, flags, t, rot, s, ex


def parse(path):
    """Returns a list of Shape with verts already in the file's root space (node transforms applied)."""
    d = open(path, "rb").read()
    return parse_bytes(d)


def parse_bytes(d):
    bs, types, sizes, strs, p = read_header(d)
    offs = []
    for s in sizes:
        offs.append(p); p += s
    # nodes: children, transforms
    node = {}
    shape = {}
    for i, t in enumerate(types):
        if t in ("NiNode", "BSMultiBoundNode", "BSFadeNode", "BSOrderedNode", "BSValueNode", "BSLeafAnimNode", "BSTreeNode"):
            r = R(d, offs[i])
            name, fl, tr, rot, sc, _ = _avobject(r, strs)
            n = r.u32(); ch = [r.i32() for _ in range(n)]
            node[i] = (name, tr, rot, sc, ch)
    parent = {}
    for i, (_, _, _, _, ch) in node.items():
        for c in ch:
            parent[c] = i

    def world(i, v):
        while i in parent:
            i = parent[i]
            _, tr, rot, sc, _ = node[i]
            m = np.array(rot, dtype=np.float32).reshape(3, 3)
            v = (v @ m.T) * sc + np.array(tr, dtype=np.float32)
        return v

    out = []
    for i, t in enumerate(types):
        if t in ("BSTriShape", "BSSubIndexTriShape", "BSMeshLODTriShape", "BSDynamicTriShape"):
            r = R(d, offs[i])
            name, fl, tr, rot, sc, _ = _avobject(r, strs)
            r.fs(4)  # sphere
            r.i32()
            shader = r.i32(); alpha = r.i32()
            vdesc = struct.unpack_from("<Q", d, r.p)[0]; r.p += 8
            ntri = r.u16(); nv = r.u16(); dsize = r.u32()
            s = Shape(); s.name = name; s.kind = t
            if dsize and nv:
                vsize = (vdesc & 0xF) * 4
                uv_off = ((vdesc >> 8) & 0xF) * 4
                flags = (vdesc >> 44) & 0xFFF
                has_v = flags & 0x1; has_uv = flags & 0x2; has_col = flags & 0x20
                col_off = ((vdesc >> 24) & 0xF) * 4
                # SSE (bs 100) always stores 32-bit positions, whatever the flag says
                full = True
                buf = np.frombuffer(d, dtype=np.uint8, count=vsize * nv, offset=r.p).reshape(nv, vsize)
                r.p += vsize * nv
                if full:
                    v = np.frombuffer(buf[:, :12].tobytes(), dtype="<f4").reshape(nv, 3).copy()
                else:
                    v = np.frombuffer(buf[:, :6].tobytes(), dtype="<f2").reshape(nv, 3).astype(np.float32)
                m = np.array(rot, dtype=np.float32).reshape(3, 3)
                v = (v @ m.T) * sc + np.array(tr, dtype=np.float32)
                s.verts = world(i, v)
                if has_uv:
                    s.uvs = np.frombuffer(buf[:, uv_off:uv_off + 4].tobytes(), dtype="<f2").reshape(nv, 2).astype(np.float32)
                if has_col:
                    s.colors = buf[:, col_off:col_off + 4].copy()
                s.tris = np.frombuffer(d, dtype="<u2", count=ntri * 3, offset=r.p).reshape(ntri, 3).copy()
            _shader(d, offs, types, strs, shader, alpha, s)
            out.append(s)
        elif t == "NiTriShape":
            r = R(d, offs[i])
            name, fl, tr, rot, sc, _ = _avobject(r, strs)
            data = r.i32(); r.i32()  # data, skin
            # material data (SSE: no materials), shader, alpha
            nm = r.u32(); r.p += nm * 8
            r.i32(); r.u8()
            shader = r.i32(); alpha = r.i32()
            s = Shape(); s.name = name; s.kind = t
            r2 = R(d, offs[data])
            r2.i32()  # group id
            nv = r2.u16(); r2.u8(); r2.u8()
            hv = r2.u8()
            v = np.frombuffer(d, dtype="<f4", count=nv * 3, offset=r2.p).reshape(nv, 3).copy(); r2.p += nv * 12
            bsv = r2.u16()
            r2.u32()  # material CRC
            has_n = r2.u8()
            if has_n:
                r2.p += nv * 12
                if bsv & 0xF000:
                    r2.p += nv * 24
            r2.fs(4)
            has_c = r2.u8()
            if has_c:
                s.colors = (np.frombuffer(d, dtype="<f4", count=nv * 4, offset=r2.p).reshape(nv, 4) * 255).astype(np.uint8); r2.p += nv * 16
            if bsv & 1:
                s.uvs = np.frombuffer(d, dtype="<f4", count=nv * 2, offset=r2.p).reshape(nv, 2).copy(); r2.p += nv * 8
            r2.u16(); r2.i32()
            r2.p += 0
            ntri = r2.u16(); r2.u32(); ht = r2.u8()
            if ht:
                s.tris = np.frombuffer(d, dtype="<u2", count=ntri * 3, offset=r2.p).reshape(ntri, 3).copy()
            m = np.array(rot, dtype=np.float32).reshape(3, 3)
            v = (v @ m.T) * sc + np.array(tr, dtype=np.float32)
            s.verts = world(i, v)
            _shader(d, offs, types, strs, shader, alpha, s)
            out.append(s)
    return out


def read_btt(path):
    """Tree LOD: {type index: [(x, y, z, rot, scale, refid), ...]} in tile-local units."""
    d = open(path, "rb").read()
    n = struct.unpack_from("<I", d, 0)[0]
    p = 4
    out = {}
    for _ in range(n):
        t, c = struct.unpack_from("<II", d, p); p += 8
        a = np.frombuffer(d, dtype="<f4", count=c * 8, offset=p).reshape(c, 8).copy(); p += c * 32
        out[t] = a[:, :5]
    return out


def read_lst(path):
    """Tree billboard list: {index: (width, height, u0, v0, u1, v1)}."""
    d = open(path, "rb").read()
    n = struct.unpack_from("<I", d, 0)[0]
    p = 4
    out = {}
    for _ in range(n):
        i = struct.unpack_from("<I", d, p)[0]
        w, h, u0, v0, u1, v1 = struct.unpack_from("<6f", d, p + 4)
        out[i] = (w, h, u0, v0, u1, v1)
        p += 32
    return out


def _shader(d, offs, types, strs, shader, alpha, s):
    s.alpha = None
    if 0 <= alpha < len(types):
        r = R(d, offs[alpha])
        r.i32(); n = r.u32(); r.p += 4 * n; r.i32()
        s.alpha = (r.u16(), r.u8())
    if not (0 <= shader < len(types)):
        return
    if types[shader] == "BSLightingShaderProperty":
        r = R(d, offs[shader])
        r.u32()  # shader type
        r.i32(); n = r.u32(); r.p += 4 * n; r.i32()
        f1 = r.u32(); f2 = r.u32()
        s.sflags = (f1, f2)
        r.fs(4)  # uv offset + scale
        ts = r.i32()
        s.emissive = r.fs(3) + (r.f(),)
        if 0 <= ts < len(types):
            r3 = R(d, offs[ts])
            nt = r3.u32()
            s.textures = [r3.sized() for _ in range(nt)]
    elif types[shader] == "BSEffectShaderProperty":
        r = R(d, offs[shader])
        r.i32(); n = r.u32(); r.p += 4 * n; r.i32()
        r.u32(); r.u32(); r.fs(4)
        s.effect_tex = r.sized()
