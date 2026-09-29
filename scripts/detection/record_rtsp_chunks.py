"""
RTSP Chunk Recorder
===================
Records a camera stream to disk in fixed-length chunks (default 10 minutes)
using ffmpeg stream copy (no re-encode, negligible CPU).

Two ways to point it at a camera:

  1. Camview / DSS camera (token auth — what the DB cameras use):
       python record_rtsp_chunks.py --server d15.camview.io --device 1000017 --channel 21

     The DSS server does NOT accept user:pass inside the rtsp:// URL. It needs:
       authorize (2-step MD5) -> token -> MTS.Video.StartVideo -> rtsp URL + ?token=
     plus a keepalive every ~22s or the session (and stream) dies. This script
     does all of that and re-fetches a fresh URL/token whenever ffmpeg drops.
     Credentials come from CAMVIEW_AUTH_USER / CAMVIEW_AUTH_PASS (env or script-server/.env).

  2. Plain RTSP camera/NVR with basic/digest auth:
       python record_rtsp_chunks.py --url "rtsp://host:554/stream" --user admin --password 'p@ss'
     (--user/--password are URL-encoded for you, so special chars like @ # : are safe.)

Output: <out>/<label>/<label>_YYYYmmdd_HHMMSS.mp4  (fragmented MP4 — a chunk
cut short by a crash/Ctrl+C is still playable).

Stop with Ctrl+C / SIGTERM; the current chunk is finalized cleanly.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("record_rtsp")

# ============================================================
# Configuration
# ============================================================
TIMEOUT_API = 10
KEEPALIVE_SECS = 22          # DSS session times out ~30s after last keepalive
RTSP_TIMEOUT_US = 15_000_000  # ffmpeg socket timeout (µs) — detect dead streams
RETRY_DELAY = 10

STOP = threading.Event()


def _load_env_file():
    """Pick up CAMVIEW_AUTH_* from script-server/.env if not already in the environment."""
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            m = re.match(r'^\s*(CAMVIEW_AUTH_USER|CAMVIEW_AUTH_PASS)\s*=\s*(.*?)\s*$', line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip('"').strip("'")


# ============================================================
# DSS / Camview Authentication
# ============================================================
def generate_signature(userName, password, realm, randomKey):
    temp = hashlib.md5(password.encode()).hexdigest()
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    temp = hashlib.md5(temp.encode()).hexdigest()
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    return hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()


def authenticate_server(server, user, password):
    """Two-step Camview authentication. Returns token or None."""
    auth_url = f"https://{server}/admin/API/accounts/authorize"
    payload = {"userName": user, "ipAddress": "", "clientType": "WINPC_V1"}
    try:
        res = requests.post(auth_url, json=payload, timeout=TIMEOUT_API, verify=False)
        j = res.json()
    except Exception as e:
        logger.error(f"Auth step 1 failed for {server}: {e}")
        return None

    realm, randomKey, publicKey = j.get("realm"), j.get("randomKey"), j.get("publickey")
    if not all([realm, randomKey, publicKey]):
        logger.error(f"Auth step 1 bad response from {server}: {j}")
        return None

    payload2 = {
        "mac": "", "signature": generate_signature(user, password, realm, randomKey),
        "userName": user, "randomKey": randomKey, "publicKey": publicKey,
        "encryptType": "MD5", "ipAddress": "", "clientType": "WINPC_V1", "userType": "0"
    }
    headers2 = {"Content-Type": "application/json",
                "Cookie": res.headers.get("Set-Cookie", ""), "User-Agent": "WINPC_V1"}
    try:
        res2 = requests.post(auth_url, headers=headers2, data=json.dumps(payload2),
                             timeout=TIMEOUT_API, verify=False)
        j2 = res2.json()
    except Exception as e:
        logger.error(f"Auth step 2 failed for {server}: {e}")
        return None

    if j2.get("code") not in (None, 1000, 0) or not j2.get("token"):
        # code 2002 = user locked — don't hammer the server
        logger.error(f"Auth rejected by {server}: {j2.get('desc')} (code {j2.get('code')})")
        return None
    return j2["token"]


class Session:
    """Holds a DSS token and keeps it alive in the background."""

    def __init__(self, server, user, password):
        self.server, self.user, self.password = server, user, password
        self.token = None
        self._lock = threading.Lock()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def login(self):
        token = authenticate_server(self.server, self.user, self.password)
        with self._lock:
            self.token = token
        return token

    def _keepalive_loop(self):
        while not STOP.wait(KEEPALIVE_SECS):
            with self._lock:
                token = self.token
            if not token:
                continue
            try:
                r = requests.put(f"https://{self.server}/admin/API/accounts/keepalive",
                                 headers={"Content-Type": "application/json", "X-Subject-Token": token},
                                 json={}, timeout=5, verify=False)
                if r.status_code == 401:
                    logger.warning("Keepalive 401 — session expired, will re-login on next reconnect")
                    with self._lock:
                        self.token = None
            except Exception:
                pass

    def start_video(self, device_id, channel, stream_type="1"):
        """Returns list of rtsp URLs (with ?token=) or None.
        stream_type: "1" = main stream (full quality), "2" = sub stream."""
        with self._lock:
            token = self.token
        if not token:
            token = self.login()
            if not token:
                return None

        url = f"https://{self.server}/admin/API/MTS/Video/StartVideo?token={token}"
        payload = {
            "clientType": "WINPC_V1", "project": "PSDK", "method": "MTS.Video.StartVideo",
            "data": {
                "streamType": stream_type,
                "optional": f"/admin/API/MTS/Video/StartVideo?token={token}",
                "channelId": f"{device_id}$1$0${channel}",
                "dataType": "2",
                "enableRtsps": "0"
            }
        }
        try:
            r = requests.post(url, headers={"Content-Type": "application/json;charset=UTF-8",
                                            "X-Subject-Token": token},
                              data=json.dumps(payload), timeout=TIMEOUT_API, verify=False)
        except Exception as e:
            logger.error(f"StartVideo error: {e}")
            return None
        if r.status_code == 401:
            logger.warning("StartVideo 401 — token expired, re-login next attempt")
            with self._lock:
                self.token = None
            return None
        if r.status_code != 200:
            logger.error(f"StartVideo HTTP {r.status_code}: {r.text[:200]}")
            return None

        data = (r.json() or {}).get("data") or {}
        stream_token = data.get("token") or token
        urls = []
        for key in ("url", "url2"):
            urls.extend(p.strip() for p in (data.get(key) or "").split("|") if p.strip())
        # Private-IP URLs are the DVR's LAN address — unreachable from here.
        urls = [u for u in urls if not is_private_ip_url(u)]
        # Keep percent-encoding as returned: DSS needs %24/%26 intact in the token.
        return [f"{u}?token={stream_token}" for u in urls] or None


def is_private_ip_url(url):
    m = re.search(r'://(\d+)\.(\d+)\.\d+\.\d+', url)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


# ============================================================
# Recording
# ============================================================
def build_ffmpeg_cmd(stream_url, out_pattern, segment_secs, with_audio):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-rtsp_transport", "tcp",
        "-timeout", str(RTSP_TIMEOUT_US),
        "-fflags", "+genpts",
        "-i", stream_url,
        "-map", "0:v:0",
    ]
    if with_audio:
        # G.711 (pcm_alaw/mulaw) from DVRs can't be stream-copied into MP4 → AAC
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"]
    cmd += [
        "-c:v", "copy",
        "-f", "segment",
        "-segment_time", str(segment_secs),
        "-segment_atclocktime", "1",     # cut on wall-clock boundaries (10:00, 10:10, …)
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        out_pattern,
    ]
    return cmd


def _redact(text):
    text = re.sub(r'(token=)[^&\s]+', r'\1***', text)
    return re.sub(r'(rtsp://)[^/@\s]+@', r'\1***@', text)


def run_ffmpeg(cmd, label, max_seconds=None):
    """Run ffmpeg until it exits, STOP is set, or max_seconds elapses. Returns seconds it ran."""
    logger.info(f"[{label}] ffmpeg: {_redact(' '.join(cmd))}")
    started = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def _pump_stderr():
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                logger.warning(f"[{label}] ffmpeg: {_redact(line)}")
    threading.Thread(target=_pump_stderr, daemon=True).start()

    while proc.poll() is None:
        if STOP.is_set() or (max_seconds and time.time() - started >= max_seconds):
            proc.send_signal(signal.SIGINT)  # ffmpeg finalizes the current segment on SIGINT
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(1)

    elapsed = time.time() - started
    logger.info(f"[{label}] ffmpeg exited (code {proc.returncode}) after {elapsed:.0f}s")
    return elapsed


def main():
    p = argparse.ArgumentParser(description="Record an RTSP stream in fixed-length chunks")
    src = p.add_argument_group("Camview / DSS camera")
    src.add_argument("--server", help="DSS server, e.g. d15.camview.io")
    src.add_argument("--device", help="device_id from camera_links_rtsp")
    src.add_argument("--channel", help="camera_no from camera_links_rtsp")
    src.add_argument("--substream", action="store_true", help="Use sub stream (lower res/bitrate)")
    direct = p.add_argument_group("Plain RTSP camera")
    direct.add_argument("--url", help="rtsp:// URL (credentials may be embedded or given below)")
    direct.add_argument("--user", help="RTSP username (URL-encoded automatically)")
    direct.add_argument("--password", help="RTSP password (URL-encoded automatically)")
    p.add_argument("--out", default="recordings", help="Output root dir (default: ./recordings)")
    p.add_argument("--label", help="Folder/file prefix (default: device_channel or host)")
    p.add_argument("--segment", type=int, default=600, help="Chunk length in seconds (default 600 = 10 min)")
    p.add_argument("--duration", type=int, help="Stop after N seconds total (default: run forever)")
    p.add_argument("--audio", action="store_true", help="Also record audio (transcoded to AAC)")
    args = p.parse_args()

    if not args.url and not (args.server and args.device and args.channel):
        p.error("give either --url, or --server + --device + --channel")

    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())

    session = None
    if args.url:
        url = args.url
        if args.user:
            creds = urllib.parse.quote(args.user, safe="")
            if args.password:
                creds += ":" + urllib.parse.quote(args.password, safe="")
            url = re.sub(r'^(rtsp[s]?://)(?:[^/@]*@)?', lambda m: m.group(1) + creds + "@", url)
        label = args.label or (urllib.parse.urlparse(url).hostname or "camera")
    else:
        _load_env_file()
        user = os.environ.get("CAMVIEW_AUTH_USER", "system")
        password = os.environ.get("CAMVIEW_AUTH_PASS", "")
        if not password:
            sys.exit("CAMVIEW_AUTH_PASS not set (env or script-server/.env)")
        session = Session(args.server, user, password)
        label = args.label or f"{args.device}_{args.channel}"

    out_dir = os.path.abspath(os.path.join(args.out, label))
    os.makedirs(out_dir, exist_ok=True)
    out_pattern = os.path.join(out_dir, f"{label}_%Y%m%d_%H%M%S.mp4")
    logger.info(f"Recording {label} → {out_dir} in {args.segment}s chunks")

    deadline = time.time() + args.duration if args.duration else None
    while not STOP.is_set():
        remaining = (deadline - time.time()) if deadline else None
        if remaining is not None and remaining <= 0:
            break

        if session:
            urls = session.start_video(args.device, args.channel,
                                       stream_type="2" if args.substream else "1")
            if not urls:
                logger.warning(f"[{label}] no stream URL, retrying in {RETRY_DELAY}s")
                STOP.wait(RETRY_DELAY)
                continue
        else:
            urls = [url]

        ran = 0
        for stream_url in urls:
            ran = run_ffmpeg(build_ffmpeg_cmd(stream_url, out_pattern, args.segment, args.audio),
                             label, max_seconds=remaining)
            if STOP.is_set() or ran > 30:
                break   # it was recording — go back round for a fresh token/URL
        if not STOP.is_set() and ran <= 30:
            STOP.wait(RETRY_DELAY)

    STOP.set()
    logger.info(f"Stopped. Chunks in {out_dir}")


if __name__ == "__main__":
    main()
