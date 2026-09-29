#!/usr/bin/env python3
"""Ensure every model in models.lock.json is present in DEST with the pinned sha256.

Usage:  fetch_models.py DEST [--from SEED_DIR]
  --from  copy from a seed dir (the copy baked into the image) before trying the network.
Exit 1 if a REQUIRED model cannot be made valid (yolo26m/yolo26n + the two fine-tuned); other
models only warn, so an offline node still starts with the models it has.

"bundled" models (the fine-tuned ones) live in the repo under models_bundled/ and
are copied from there instead of downloaded — they are not hosted anywhere.
"""
import hashlib, json, os, shutil, sys, urllib.request

REQUIRED = {"yolo26m.pt", "yolo26n.pt", "monitor_person_yolo26m.pt", "best_mobile_model.pt"}
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
    bundled_dir = os.path.join(HERE, lock.get("bundled_dir", "models_bundled"))
    bundled = lock.get("bundled", {})
    for name, want in {**lock["models"], **bundled}.items():
        path = os.path.join(dest, name)
        if ok(path, want):
            continue
        got = False
        if seed and ok(os.path.join(seed, name), want):
            shutil.copy2(os.path.join(seed, name), path + ".tmp")
            os.replace(path + ".tmp", path)
            print(f"[models] {name}: seeded from image")
            got = True
        elif name in bundled:
            if ok(os.path.join(bundled_dir, name), want):
                shutil.copy2(os.path.join(bundled_dir, name), path + ".tmp")
                os.replace(path + ".tmp", path)
                print(f"[models] {name}: copied from {bundled_dir}")
                got = True
            else:
                print(f"[models] {name}: FAILED (not in image seed or {bundled_dir}, or sha256 mismatch)")
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
