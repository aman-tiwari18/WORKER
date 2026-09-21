#!/usr/bin/env python3
"""Ensure every model in models.lock.json is present in DEST with the pinned sha256.

Usage:  fetch_models.py DEST [--from SEED_DIR]
  --from  copy from a seed dir (the copy baked into the image) before trying the network.
Exit 1 if a REQUIRED model cannot be made valid (required = yolo26m/yolo26n); other
models only warn, so an offline node still starts with the models it has.
"""
import hashlib, json, os, shutil, sys, urllib.request

REQUIRED = {"yolo26m.pt", "yolo26n.pt"}
HERE = os.path.dirname(os.path.abspath(__file__))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ok(path, want):
    return os.path.isfile(path) and sha256(path) == want


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    dest = args[0]
    seed = args[args.index("--from") + 1] if "--from" in args else None
    lock = json.load(open(os.path.join(HERE, "models.lock.json")))
    os.makedirs(dest, exist_ok=True)
    failed = []
    for name, want in lock["models"].items():
        path = os.path.join(dest, name)
        if ok(path, want):
            continue
        got = False
        if seed and ok(os.path.join(seed, name), want):
            shutil.copy2(os.path.join(seed, name), path + ".tmp")
            os.replace(path + ".tmp", path)
            print(f"[models] {name}: seeded from image")
            got = True
        else:
            try:
                print(f"[models] {name}: downloading")
                urllib.request.urlretrieve(f"{lock['base_url']}/{name}", path + ".tmp")
                if sha256(path + ".tmp") != want:
                    raise ValueError("sha256 mismatch")
                os.replace(path + ".tmp", path)
                got = True
            except Exception as e:
                print(f"[models] {name}: FAILED ({e})")
                if os.path.exists(path + ".tmp"):
                    os.remove(path + ".tmp")
        if not got:
            failed.append(name)
    bad_required = [m for m in failed if m in REQUIRED]
    if failed:
        print(f"[models] missing/invalid: {failed}")
    sys.exit(1 if bad_required else 0)


if __name__ == "__main__":
    main()
