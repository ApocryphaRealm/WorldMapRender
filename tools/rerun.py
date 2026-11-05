"""Rerun World Map Render after the list's landscape changed (new xLODGen / DynDOLOD outputs, new roads, a new land).

    rerun.py [--stage <folder>] [--install "<MO2 mod folder>"] [--world <EditorID>]...

1. Lists the worlds with usable map data in the active load order and says which are not in worlds.json.
2. Renders every land in worlds.json (or only the named ones) into the stage folder: days, nights, meshes, table, marker list.
3. With --install, copies the result over the mod's folder in MO2. It refuses while Skyrim or Mod Organizer is running, because
   MO2 keeps the mod's file list while it runs. It never edits a profile.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def running(*names):
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout.lower()
    return [n for n in names if '"%s"' % n.lower() in out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default=os.path.join("C:" + os.sep, "Builds", "worldmap-render", "stage", "data"))
    ap.add_argument("--install")
    ap.add_argument("--world", action="append")
    args = ap.parse_args()
    py = sys.executable
    work = os.path.dirname(os.path.normpath(args.stage))
    os.makedirs(work, exist_ok=True)
    listing = os.path.join(work, "worlds-list.json")
    subprocess.run([py, "-I", "-X", "utf8", os.path.join(HERE, "list_worlds.py"), listing], check=True)
    have = {k.lower() for k in json.load(open(os.path.join(HERE, "worlds.json"), encoding="utf-8"))}
    for w in json.load(open(listing, encoding="utf-8")):
        if w["edid"].lower() not in have:
            print("NOT IN worlds.json:", w["edid"], "level 4 terrain tiles:", w["xlod_level4"])
    cmd = [py, "-I", "-X", "utf8", os.path.join(HERE, "build_maps.py"), "--out", args.stage, "--preview", os.path.join(work, "preview")]
    for w in args.world or []:
        cmd += ["--world", w]
    if not args.world:
        cmd.append("--all")
    subprocess.run(cmd, check=True)
    if args.install:
        busy = running("SkyrimSE.exe", "skse64_loader.exe", "ModOrganizer.exe")
        if busy:
            sys.exit("not installing while running: %s. The stage folder holds the result: %s" % (", ".join(busy), args.stage))
        for root, _, files in os.walk(args.stage):
            for f in files:
                src = os.path.join(root, f)
                dst = os.path.join(args.install, os.path.relpath(src, args.stage))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        print("installed to", args.install)


if __name__ == "__main__":
    main()
