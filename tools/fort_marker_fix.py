"""Adds one record to our own Atlas patch: Fort Dawnguard's quest marker (Dawnguard.esm 00FC6C) back at Dawnguard.esm's position.
Atlas Map Markers moved it to the valley, and Atlas Map Markers Overhaul named it Fort Dawnguard there. The quests use this marker, so it
is moved rather than hidden. Only the position changes. The result is written to the given file; the installed plugin is left alone.

    fort_marker_fix.py <out esp>
"""
import os, struct, sys
os.environ.setdefault("WMR_PROFILE", "Njordlinger Test")
import markers as M

PATCH = "Atlas Duplicate Markers Off.esp"
SOURCE = "atlas map markers overhaul.esp"
FORM = 0x00FC6C


def sub_at(body, tag):
    p = 0
    while p + 6 <= len(body):
        t, n = body[p:p + 4], struct.unpack_from("<H", body, p + 4)[0]
        if t == tag:
            return p + 6
        p += 6 + n
    raise SystemExit("no %s" % tag)


def masters_of(d):
    return [s[:-1].decode("latin1") for t, s in M.subs(d[24:24 + struct.unpack_from("<I", d, 4)[0]]) if t == b"MAST"]


def dawnguard_position(mods):
    d = open(M.find_plugin("Dawnguard.esm", mods), "rb").read()
    pat = struct.pack("<I", (len(masters_of(d)) << 24) | FORM)
    p = 0
    while True:
        p = d.find(pat, p)
        if p < 0:
            raise SystemExit("marker not found in Dawnguard.esm")
        if d[p - 12:p - 8] == b"REFR":
            body = d[p + 12:p + 12 + struct.unpack_from("<I", d, p - 8)[0]]
            at = sub_at(body, b"DATA")
            return body[at:at + 24]
        p += 4


def world_block(d, want, position):
    """The marker with its world and cell above it, as the source has them, the position changed."""
    found = None

    def walk(p, end, stack):
        nonlocal found
        last = None
        while p + 24 <= end and not found:
            if d[p:p + 4] == b"GRUP":
                gs = struct.unpack_from("<I", d, p + 4)[0]
                walk(p + 24, p + gs, stack + [(d[p:p + 24], last)])
                p += gs
                continue
            sz, fid = struct.unpack_from("<I", d, p + 4)[0], struct.unpack_from("<I", d, p + 12)[0]
            if d[p:p + 4] == b"REFR" and fid == want:
                found = (stack, d[p:p + 24 + sz])
                return
            last = d[p:p + 24 + sz]
            p += 24 + sz

    p = 24 + struct.unpack_from("<I", d, 4)[0]
    while p + 24 <= len(d) and not found:
        gs = struct.unpack_from("<I", d, p + 4)[0]
        if d[p + 8:p + 12] == b"WRLD":
            walk(p + 24, p + gs, [])
        p += gs
    if not found:
        raise SystemExit("marker not found in " + SOURCE)
    stack, refr = found
    body = bytearray(refr[24:])
    at = sub_at(bytes(body), b"DATA")
    body[at:at + 24] = position
    blob = refr[:24] + bytes(body)
    for head, parent in reversed(stack):
        g = bytearray(head)
        struct.pack_into("<I", g, 4, 24 + len(blob))
        blob = bytes(g) + blob
        if parent:
            blob = parent + blob
    return blob


def main():
    out = sys.argv[1]
    mods = M.active_mods()
    src = open(M.find_plugin(SOURCE, mods), "rb").read()
    d = open(M.find_plugin(PATCH, mods), "rb").read()
    a, b = masters_of(src), masters_of(d)
    if [m.lower() for m in a] != [m.lower() for m in b[:len(a)]] or b[len(a)].lower() != SOURCE:
        raise SystemExit("masters differ: %s %s" % (a, b))
    index = next(i for i, m in enumerate(a) if m.lower() == "dawnguard.esm")
    block = world_block(src, (index << 24) | FORM, dawnguard_position(mods))
    size = struct.unpack_from("<I", d, 4)[0]
    top = 24 + size
    if d[top:top + 4] != b"GRUP" or d[top + 8:top + 12] != b"WRLD":
        raise SystemExit("the patch's first group is not its worlds")
    gs = struct.unpack_from("<I", d, top + 4)[0]
    if struct.pack("<I", (index << 24) | FORM) in d:
        raise SystemExit("the patch already has this marker")
    new = bytearray(d[:top + gs] + block + d[top + gs:])
    struct.pack_into("<I", new, top + 4, gs + len(block))
    # the header's record count: world, cell and marker
    hp = 24 + 6
    v, n, nxt = struct.unpack_from("<fII", new, hp)
    struct.pack_into("<fII", new, hp, v, n + 3, nxt)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "wb").write(bytes(new))
    print("wrote", out, len(new), "bytes, was", len(d), "records", n, "to", n + 3, "position", struct.unpack("<6f", dawnguard_position(mods))[:3])


if __name__ == "__main__":
    main()
