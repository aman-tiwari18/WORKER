"""Minimal test runner (no pytest needed). Run from scripts/detection:

    python -m pathb.tests.run_tests            # all
    python -m pathb.tests.run_tests boxes      # modules whose name contains 'boxes'
"""
import importlib
import pkgutil
import sys
import traceback

import pathb.tests as pkg


def main(filters):
    failed = passed = 0
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if not mod_info.name.startswith("test_"):
            continue
        if filters and not any(f in mod_info.name for f in filters):
            continue
        mod = importlib.import_module(f"pathb.tests.{mod_info.name}")
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            try:
                ret = getattr(mod, name)()
                passed += 1
                extra = f"  -> {ret}" if ret is not None else ""
                print(f"PASS {mod_info.name}.{name}{extra}")
            except Exception:
                failed += 1
                print(f"FAIL {mod_info.name}.{name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
