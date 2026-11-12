"""Commit one complete map build into the local assets repo (WorldMapRender-Textures), so a good build can always be found.

    snapshot_textures.py <build folder> -m "<where it came from, repo commit, date, what it fixes>" [--tag NAME] [--repo DIR]

<build folder> is a folder laid out like the MO2 mod folder (a stage\\data, the repo dist, a test folder, a backup).
The repo's working tree is REPLACED by that build's textures\\, meshes\\ and the SKSE\\Plugins data tables (*.txt, *.json:
the land table, tile list and marker list the pictures were drawn for); the DLL/PDB are code and live in the code repo.
A file the build lacks is removed from the tree, so each commit is the whole build, not an overlay.
Nothing is committed when the build equals the last commit. The repo is local only: it is never pushed.

Why (2026-11-12): the hand-smoothed pictures of 2026-11-05 lived only in C:\\Builds\\worldmap-render\\stage\\data; the
package was built from the older repo dist, and two full installs of that package reverted the maps.
build_maps.py and retouch.py call this at the end of a run (--no-snapshot skips it).
"""
import argparse, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", "WorldMapRender-Textures"))
IDENT = ["-c", "user.name=ApocryphaRealm", "-c", "user.email=37273616+ApocryphaRealm@users.noreply.github.com"]
PARTS = ("textures", "meshes", os.path.join("SKSE", "Plugins"))


def wanted(rel):
    low = rel.lower().replace("\\", "/")
    if low.startswith(("textures/", "meshes/")):
        return True
    return low.startswith("skse/plugins/") and low.endswith((".txt", ".json")) and low.count("/") == 2


def files(root):
    out = {}
    for part in PARTS:
        base = os.path.join(root, part)
        for dp, dn, fn in os.walk(base):
            for f in fn:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, root)
                if wanted(rel):
                    out[os.path.normcase(rel)] = (rel, full)
    return out


def git(repo, *a, check=True):
    r = subprocess.run(["git", "-C", repo] + IDENT + list(a), capture_output=True, text=True)
    if check and r.returncode:
        sys.exit("git %s failed: %s" % (" ".join(a), r.stderr.strip()))
    return r.stdout.strip()


def snapshot(build, message, tag=None, repo=REPO):
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit("no assets repo at %s" % repo)
    src = files(build)
    if not any(k.startswith("textures") for k in src):
        sys.exit("REFUSED: %s holds no textures\\ - not a map build" % build)
    have = files(repo)
    for k, (rel, full) in have.items():
        if k not in src:
            os.remove(full)
    for k, (rel, full) in src.items():
        to = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(to), exist_ok=True)
        shutil.copy2(full, to)
    for part in PARTS:   # empty folders left by removed lands
        for dp, dn, fn in sorted(os.walk(os.path.join(repo, part)), reverse=True):
            if not dn and not fn and dp != os.path.join(repo, part):
                os.rmdir(dp)
    git(repo, "add", "-A", "--", ".")
    if not git(repo, "status", "--porcelain"):
        print("unchanged: the build equals", git(repo, "rev-parse", "--short", "HEAD"))
    else:
        git(repo, "commit", "-q", "-m", message)
        print("committed", git(repo, "rev-parse", "--short", "HEAD"), "-", len(src), "files from", build)
    if tag:
        git(repo, "tag", "-f", tag)
        print("tagged", tag)
    return git(repo, "rev-parse", "--short", "HEAD")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("build")
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("--tag")
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args()
    snapshot(a.build, a.message, a.tag, a.repo)
