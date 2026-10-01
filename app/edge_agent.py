"""
edge_agent.py — companion container for a server OUTSIDE the LAN (cloud, other site).

A remote server reaches the main server only over the public HTTPS URL (no NFS, no
LAN ports), and the main server cannot reach it at all. One edge-agent per server
does the three LAN-only jobs over HTTPS instead, with the server's own key:

  1. DVR tokens  — listens on :8090 inside the compose network (workers use
     CAMVIEW_TOKEN_SERVER=http://edge-agent:8090) and forwards GET /token and
     POST /evict to the backend, which asks the shared token server on the main host.
     All workers everywhere share one DVR session, as on the LAN.
  2. Frames      — uploads every new file under FRAMES_DIR (the workers' results
     folder) to /fleet/frames, so detection images show in the portal; keeps local
     copies for FRAME_RETENTION_H hours.
  3. Metrics     — every METRICS_PUSH_S seconds pushes each worker's /metrics and the
     node-exporter's to /fleet/metrics; the main Prometheus scrapes them from there.

Env: MAIN_BACKEND_URL, API_KEY (from .env), FRAMES_DIR, METRICS_TARGETS
("worker-id=host:port,..."), NODE_TARGET ("host:port" or empty), WORKER_HOSTNAME.
"""
import gzip
import json
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

BACKEND = os.environ["MAIN_BACKEND_URL"].rstrip("/")
HEADERS = {"X-API-Key": os.environ["API_KEY"]}
FRAMES_DIR = os.environ.get("FRAMES_DIR", "/frames").rstrip("/")
RETENTION_S = float(os.environ.get("FRAME_RETENTION_H", "24")) * 3600
SCAN_S = float(os.environ.get("FRAME_SCAN_S", "3"))
UPLOAD_THREADS = int(os.environ.get("FRAME_UPLOAD_THREADS", "4"))
METRICS_PUSH_S = float(os.environ.get("METRICS_PUSH_S", "15"))
TOKEN_PORT = int(os.environ.get("EDGE_TOKEN_PORT", "8090"))
HOSTNAME = os.environ.get("WORKER_HOSTNAME") or os.uname().nodename
INDEX = os.path.join(FRAMES_DIR, ".uploaded.idx")   # relpath \t size \t mtime, one per upload

client = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), headers=HEADERS)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[edge-agent]", msg, flush=True)


# ── 1. DVR token relay ───────────────────────────────────────────────────────
class TokenRelay(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/health":
            return self._send(200, b'{"ok": true}')
        if url.path != "/token":
            return self._send(404, b"{}")
        try:
            r = client.get(f"{BACKEND}/fleet/camview/token", params=urllib.parse.parse_qsl(url.query), timeout=100)
            return self._send(r.status_code, r.content, r.headers.get("content-type", "application/json"))
        except Exception as exc:
            return self._send(502, json.dumps({"token": None, "error": str(exc)}).encode())

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/evict":
            return self._send(404, b"{}")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
        try:
            r = client.post(f"{BACKEND}/fleet/camview/evict", content=body,
                            headers={"Content-Type": "application/json"}, timeout=20)
            return self._send(r.status_code, r.content, r.headers.get("content-type", "application/json"))
        except Exception as exc:
            return self._send(502, json.dumps({"ok": False, "error": str(exc)}).encode())

    def log_message(self, *args):  # quiet: workers ask often
        pass


# ── 2. frames ────────────────────────────────────────────────────────────────
def load_index():
    done = {}
    try:
        with open(INDEX) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3:
                    done[parts[0]] = (int(parts[1]), float(parts[2]))
    except FileNotFoundError:
        pass
    return done


def upload(rel, size, mtime):
    path = os.path.join(FRAMES_DIR, rel)
    with open(path, "rb") as f:
        data = f.read()
    for attempt in range(4):
        try:
            r = client.post(f"{BACKEND}/fleet/frames", params={"path": rel}, content=data,
                            headers={"Content-Type": "application/octet-stream"})
            if r.status_code == 200:
                return rel, size, mtime
            if r.status_code in (400, 413):          # never accepted: do not retry forever
                log(f"frame {rel} refused: HTTP {r.status_code} {r.text[:120]}")
                return rel, size, mtime
        except Exception as exc:
            if attempt == 3:
                log(f"frame {rel}: upload failed ({exc}) — retried next scan")
        time.sleep(2 ** attempt)
    return None


def frames_loop():
    done = load_index()
    idx = open(INDEX, "a")
    pool = ThreadPoolExecutor(UPLOAD_THREADS)
    last_prune = 0.0
    while True:
        now = time.time()
        todo = []
        for root, dirs, files in os.walk(FRAMES_DIR):
            for name in files:
                if name.startswith(".") or ".part" in name:
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue
                if now - st.st_mtime < 2:             # still being written
                    continue
                rel = os.path.relpath(path, FRAMES_DIR)
                if done.get(rel) == (st.st_size, st.st_mtime):
                    continue
                todo.append((rel, st.st_size, st.st_mtime))
        for res in pool.map(lambda t: upload(*t), todo):
            if res:
                done[res[0]] = (res[1], res[2])
                idx.write(f"{res[0]}\t{res[1]}\t{res[2]}\n")
        idx.flush()
        if todo:
            log(f"uploaded {sum(1 for _ in todo)} frame(s)")
        # prune local copies already uploaded and older than the retention
        if now - last_prune > 600:
            last_prune = now
            for rel, (size, mtime) in list(done.items()):
                if now - mtime > RETENTION_S:
                    try:
                        os.remove(os.path.join(FRAMES_DIR, rel))
                    except FileNotFoundError:
                        pass
                    done.pop(rel, None)
            idx.close()
            with open(INDEX + ".tmp", "w") as f:
                for rel, (size, mtime) in done.items():
                    f.write(f"{rel}\t{size}\t{mtime}\n")
            os.replace(INDEX + ".tmp", INDEX)
            idx = open(INDEX, "a")
        time.sleep(SCAN_S)


# ── 3. metrics ───────────────────────────────────────────────────────────────
def metrics_loop():
    targets = [t.split("=", 1) for t in os.environ.get("METRICS_TARGETS", "").split(",") if "=" in t]
    node = os.environ.get("NODE_TARGET", "").strip()
    jobs = [("worker", wid, hp) for wid, hp in targets] + ([("node", HOSTNAME, node)] if node else [])
    log(f"pushing metrics every {METRICS_PUSH_S:.0f}s for: {', '.join(f'{k}:{i}' for k, i, _ in jobs) or 'nothing'}")
    failing = set()
    while True:
        for kind, ident, hostport in jobs:
            try:
                text = httpx.get(f"http://{hostport}/metrics", timeout=10).content
                r = client.post(f"{BACKEND}/fleet/metrics", params={"kind": kind, "id": ident},
                                content=gzip.compress(text),
                                headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"})
                r.raise_for_status()
                failing.discard(ident)
            except Exception as exc:
                if ident not in failing:      # log once per outage, not every 15 s
                    log(f"metrics {kind}:{ident} not pushed: {exc}")
                    failing.add(ident)
        time.sleep(METRICS_PUSH_S)


def main():
    os.makedirs(FRAMES_DIR, exist_ok=True)
    log(f"backend {BACKEND} | frames {FRAMES_DIR} | DVR token relay on :{TOKEN_PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", TOKEN_PORT), TokenRelay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=metrics_loop, daemon=True).start()
    frames_loop()


if __name__ == "__main__":
    main()
