"""lifecycle._running_assignments with a shift session that still lists finished members. Run: python test_lifecycle_session_members.py"""
import os, sys
from types import SimpleNamespace
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("MAIN_BACKEND_URL", "http://x"); os.environ.setdefault("API_KEY", "k"); os.environ.setdefault("WORKER_ID", "w")
import app.poller as P
import app.lifecycle as L

fails = 0
def check(n, c, x=""):
    global fails; fails += 0 if c else 1; print("PASS" if c else "FAIL", n, x)

def run(members, status, running=("ses-40-151-2026-10-28",)):
    svc = SimpleNamespace(running_scripts=set(running), group_members={"ses-40-151-2026-10-28": members},
                          sessions={"ses-40-151-2026-10-28": {"members": {m: {} for m in members}, "status": status}})
    P.poller_service = svc
    return L.lifecycle_reconciler._running_assignments() if hasattr(L, "lifecycle_reconciler") else L.LifecycleReconciler()._running_assignments()

old = ["9743", "9744"]; new = ["9780", "9781"]
m = run(old + new, {i: "COMPLETED" for i in old})
check("finished members of an earlier run are not asked about", sorted(m) == [9780, 9781], str(sorted(m)))
m = run(new, {})
check("a fresh session asks about all members", sorted(m) == [9780, 9781])
m = run(old, {i: "COMPLETED" for i in old})
check("all members done: still asks about all (so the job is stopped)", sorted(m) == [9743, 9744])
m = run(new, {"9780": "COMPLETED"})
check("one analytic done, one live: asks only about the live one", sorted(m) == [9781])
m = run(["77"], {}, running=("77",))
check("a plain (non-session) job is unchanged", m == {77: "77"}, str(m))
print("\n%d FAILED" % fails if fails else "\nall passed"); sys.exit(1 if fails else 0)
