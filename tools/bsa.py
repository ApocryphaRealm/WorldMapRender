"""Reads single files out of Skyrim's BSA archives (version 104 zlib, 105 LZ4), for textures only the archives hold."""
import glob
import io
import os
import struct
import zlib

try:
    import lz4.frame as lz4f
except ImportError:
    lz4f = None


class Archive:
    def __init__(self, path):
        self.path = path
        self.files = {}
        with open(path, "rb") as f:
            head = f.read(36)
            if head[:4] != b"BSA\0":
                raise ValueError("not a BSA")
            ver, off, flags, nfolders, nfiles, tfold, tfile, _ = struct.unpack("<8I", head[4:36])
            if flags & 0x40:
                pass
            self.ver, self.flags = ver, flags
            rec = 24 if ver >= 105 else 16
            folders = f.read(rec * nfolders)
            name_block = []
            entries = []
            for i in range(nfolders):
                if ver >= 105:
                    _, count, _, offset = struct.unpack_from("<QIIQ", folders, i * rec)
                else:
                    _, count, offset = struct.unpack_from("<QII", folders, i * rec)
                n = f.read(1)[0]
                folder = f.read(n)[:-1].decode("latin1").lower()
                block = f.read(16 * count)
                for j in range(count):
                    _, size, foff = struct.unpack_from("<QII", block, j * 16)
                    entries.append((folder, size, foff))
            names = f.read(tfile).split(b"\0")
            for (folder, size, foff), name in zip(entries, names):
                self.files[folder + "\\" + name.decode("latin1").lower()] = (size, foff)

    def read(self, rel):
        size, off = self.files[rel]
        flipped = bool(size & 0x40000000)
        size &= 0x3FFFFFFF
        compressed = bool(self.flags & 0x4) != flipped
        with open(self.path, "rb") as f:
            f.seek(off)
            if self.flags & 0x100:
                n = f.read(1)[0]
                f.read(n)
                size -= n + 1
            if not compressed:
                return f.read(size)
            orig = struct.unpack("<I", f.read(4))[0]
            data = f.read(size - 4)
        if self.ver >= 105:
            return lz4f.decompress(data)
        return zlib.decompress(data)


class Library:
    """The list's archives, winners first: active plugins' own archives later in load order beat earlier, game archives last."""

    def __init__(self, mods_root, active_mods, stock_data, plugins):
        self.archives = []
        seen = set()
        stems = [os.path.splitext(p)[0].lower() for p in plugins]
        paths = {}
        for m in reversed(active_mods):
            for f in glob.glob(glob.escape(os.path.join(mods_root, m)) + "/*.bsa"):
                paths[os.path.basename(f).lower()] = f
        for f in glob.glob(glob.escape(stock_data) + "/*.bsa"):
            paths.setdefault(os.path.basename(f).lower(), f)
        ordered = []
        for stem in reversed(stems):
            for n in list(paths):
                if n == stem + ".bsa" or n.startswith(stem + " - "):
                    ordered.append(paths.pop(n))
        for n in sorted(paths):
            if n.startswith("skyrim") or n.startswith("update") or n.startswith("dawnguard") or n.startswith("hearthfires") or n.startswith("dragonborn"):
                ordered.append(paths[n])
        for p in ordered:
            try:
                self.archives.append(Archive(p))
            except Exception as e:
                print("cannot read archive", p, e)

    def find(self, rel):
        for a in self.archives:
            if rel in a.files:
                return a
        return None
