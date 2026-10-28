"""Pass-1 concurrency (vision_classification_rtsp): interleave across DVRs + per-DVR / per-Camview-host caps.
Fake cameras and a fake capture (no network). Run: python test_vision_pass1_concurrency.py"""
import asyncio
import collections
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "utils"))

import vision_classification_rtsp as V          # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    fails += 0 if cond else 1
    print("PASS" if cond else "FAIL", name, extra)


def cameras(devices=30, per_device=20, hosts=4):
    return [{"device_id": str(1000000 + d), "camera_no": str(n), "cctv_url": f"d{11 + d % hosts}.camview.io",
             "camera_id": f"{1000000 + d}_{n}"} for d in range(devices) for n in range(per_device)]


# ---- interleave
cams = cameras(devices=3, per_device=2)
order = [c["device_id"] + ":" + c["camera_no"] for c in V._interleave_by_device(cams)]
check("interleave: one camera of each DVR before the second of any", order == ["1000000:0", "1000001:0", "1000002:0", "1000000:1", "1000001:1", "1000002:1"], str(order))
check("interleave: keeps every camera once", len(V._interleave_by_device(cameras())) == 600 and len({id(c) for c in V._interleave_by_device(cameras())}) == 600)
check("interleave: empty list", V._interleave_by_device([]) == [])


async def run(n_net, n_cap, per_dev, per_host, interleave, delay=0.5):
    V.PASS1_INTERLEAVE, V.MAX_PER_DEVICE, V.MAX_PER_CAMVIEW_HOST = interleave, per_dev, per_host
    V._PASS1_SEMS.clear()
    V._RTSP_CAP_SEM = asyncio.Semaphore(n_cap)
    cur = collections.Counter(); peak = collections.Counter(); glob = {"cur": 0, "peak": 0}

    async def fake(camera, cam_id, frames_dir):
        d, h = camera["device_id"], camera["cctv_url"]
        cur[d] += 1; cur[h] += 1; glob["cur"] += 1
        peak[d] = max(peak[d], cur[d]); peak[h] = max(peak[h], cur[h]); glob["peak"] = max(glob["peak"], glob["cur"])
        await asyncio.sleep(delay)
        cur[d] -= 1; cur[h] -= 1; glob["cur"] -= 1
        return b"jpeg", "/x.jpg"

    V._camview_rtsp_capture_and_save = fake
    sem = asyncio.Semaphore(n_net)
    cams = V._interleave_by_device(cameras()) if interleave else cameras()
    t = time.time()
    res = await asyncio.gather(*[V._check_rtsp_camera(c, sem, "") for c in cams])
    dt = time.time() - t
    dev_peak = max(v for k, v in peak.items() if k.startswith("10"))
    host_peak = max(v for k, v in peak.items() if k.endswith(".io"))
    return dt, glob["peak"], dev_peak, host_peak, sum(r["accessible"] for r in res)


t0, g0, d0, h0, a0 = asyncio.run(run(16, 6, 0, 0, False))
check("today's settings (16 / 6): all 600 checked", a0 == 600, f"{t0:.1f}s, peak {g0} at once, up to {d0} on one DVR")
t1, g1, d1, h1, a1 = asyncio.run(run(128, 64, 2, 32, True))
check("new settings (128 / 64, 2 per DVR, 32 per Camview server): all 600 checked", a1 == 600, f"{t1:.1f}s, peak {g1}, per DVR {d1}, per host {h1}")
check("per-DVR cap respected", d1 <= 2, f"max on one DVR {d1}")
check("per-Camview-host cap respected", h1 <= 32, f"max on one host {h1}")
check("global cap respected", g1 <= 64, f"peak {g1}")
check("at least 5x faster", t0 / t1 >= 5, f"{t0:.1f}s -> {t1:.1f}s ({t0 / t1:.1f}x)")
t2, g2, d2, h2, a2 = asyncio.run(run(128, 64, 0, 0, False))
check("same big numbers WITHOUT the new caps overload a DVR (what the old comment warned about)", d2 > 2, f"{d2} at once on one DVR")
t3, g3, d3, h3, a3 = asyncio.run(run(128, 64, 1, 8, True))
check("strict caps (1 per DVR, 8 per host): still all checked, caps respected", a3 == 600 and d3 <= 1 and h3 <= 8, f"{t3:.1f}s, per DVR {d3}, per host {h3}")
print("\n%d FAILED" % fails if fails else "\nall passed")
sys.exit(1 if fails else 0)
