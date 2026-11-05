"""Writes a map sheet's .btr: a flat picture quad over the world, a dark backdrop under it, and the tiny land shape the LOD
loader expects to find in a terrain chunk. Skyrim format 20.2.0.7 / user 12 / bs 83.
"""
import struct


def _f(*v):
    return struct.pack("<%df" % len(v), *v)


def _i(*v):
    return struct.pack("<%di" % len(v), *v)


def _s(t):
    return struct.pack("<I", len(t)) + t.encode("latin1")


IDENT = _f(1, 0, 0, 0, 1, 0, 0, 0, 1)


def _shape(name, translate, scale, data, shader, alpha):
    return _i(name, 0, -1, 14) + _f(*translate) + IDENT + _f(scale) + _i(-1, data, -1, 0, 0) + b"\0" + _i(shader, alpha)


def _quad_data(x0, x1, y0, y1, z, normals):
    verts = [(x1, y1, z), (x0, y1, z), (x0, y0, z), (x1, y0, z)]
    d = _i(0) + struct.pack("<H", 4) + b"\0\0\x01"
    for v in verts:
        d += _f(*v)
    if normals:
        d += struct.pack("<H", 0x1001) + _i(0) + b"\x01" + bytes(4 * 12 + 4 * 24)
    else:
        d += struct.pack("<H", 0x0001) + _i(0) + b"\0"
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / 2
    d += _f(cx, cy, z, r) + b"\0"
    for uv in ((1, 0), (0, 0), (0, 1), (1, 1)):
        d += _f(*uv)
    d += struct.pack("<H", 0x4000) + _i(-1)
    d += struct.pack("<HI", 2, 6) + b"\x01" + struct.pack("<6H", 0, 1, 2, 0, 2, 3) + struct.pack("<H", 0)
    return d


def _effect(tex):
    return _i(-1, 0, -1) + struct.pack("<II", 0x80000000, 1) + _f(0, 0, 1, 1) + _s(tex) + _i(3) + _f(1, 1, 1, 1, 1, 1, 1, 1, 1, 100) + _i(0)


ALPHA = _i(-1, 0, -1) + struct.pack("<HB", 0x12EC, 128)


def sheet_btr(sheet_tex, back_tex, x0, x1, y0, y1, z, back_z, land_z, land_xy, sheet_name="map sheet", back_name="map backdrop", scale=1.0):
    """Coordinates are relative to the chunk's own corner; x0..y1 are the sheet's edges."""
    types = ["BSMultiBoundNode", "NiTriShape", "NiTriShapeData", "BSLightingShaderProperty", "BSShaderTextureSet", "BSEffectShaderProperty", "NiAlphaProperty", "BSMultiBound", "BSMultiBoundAABB"]
    strings = ["chunk", "Land", sheet_name, back_name]
    blocks = []

    def add(t, b):
        blocks.append((types.index(t), b))

    # 0 root
    add("BSMultiBoundNode", _i(0, 0, -1, 14) + _f(0, 0, 0) + IDENT + _f(1) + _i(-1, 3, 1, 5, 9, 0, 13, 1))
    # 1-4 the small land shape, hidden below the sheet
    lx, ly = land_xy
    add("NiTriShape", _shape(1, (0, 0, 0), 32.0, 2, 3, -1))
    add("NiTriShapeData", _quad_data((lx - 32) / 32, (lx + 32) / 32, (ly - 32) / 32, (ly + 32) / 32, land_z / 32, False))
    add("BSLightingShaderProperty", _i(18, -1, 0, -1) + struct.pack("<II", 0x80401000, 3) + _f(0, 0, 1, 1) + _i(4) + _f(0, 0, 0, 1, 0, 1, 0, 1, 1, 1, 1, 1) + _i(0, 0))
    add("BSShaderTextureSet", _i(9) + _s(back_tex) + _s(back_tex) + _i(0, 0, 0, 0, 0, 0, 0))
    # 5-8 the sheet
    add("NiTriShape", _shape(2, (0, 0, z), scale, 6, 7, 8))
    add("NiTriShapeData", _quad_data(x0 / scale, x1 / scale, y0 / scale, y1 / scale, 0.0, True))
    add("BSEffectShaderProperty", _effect(sheet_tex))
    add("NiAlphaProperty", ALPHA)
    # 9-12 the backdrop, three sheets wide
    w, h = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    add("NiTriShape", _shape(3, (cx, cy, back_z), 1.0, 10, 11, 12))
    add("NiTriShapeData", _quad_data(-1.5 * w, 1.5 * w, -1.5 * h, 1.5 * h, 0.0, True))
    add("BSEffectShaderProperty", _effect(back_tex))
    add("NiAlphaProperty", ALPHA)
    # 13-14
    add("BSMultiBound", _i(14))
    add("BSMultiBoundAABB", _f(0, 0, 0, 2000000, 2000000, 2000000))

    out = b"Gamebryo File Format, Version 20.2.0.7\n" + struct.pack("<IBIII", 0x14020007, 1, 12, len(blocks), 83)
    out += b"\x01\0\x01\0\x01\0"
    out += struct.pack("<H", len(types))
    for t in types:
        out += _s(t)
    for t, _ in blocks:
        out += struct.pack("<H", t)
    for _, b in blocks:
        out += struct.pack("<I", len(b))
    out += struct.pack("<II", len(strings), max(len(s) for s in strings))
    for s in strings:
        out += _s(s)
    out += _i(0)
    for _, b in blocks:
        out += b
    out += _i(1, 0)
    return out
