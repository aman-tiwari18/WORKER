import json

import requests

from uptime.camview import Camview, TokenClient, host_port, is_private_ip_url

URLS = ("rtsp://10.0.0.5:9100/dss/monitor/param/cameraid=1000796%241%240%2417&substream=1|"
        "rtsp://203.0.113.7:9090/dss/monitor/param/cameraid=1000796%241%240%2417&substream=1|"
        "rtsp://203.0.113.7:9100/dss/monitor/param/cameraid=1000796%241%240%2417&substream=1")


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeTokens:
    def __init__(self, seq):
        self.seq, self.calls, self.dropped = list(seq), [], []

    def get(self, server, refresh=False):
        self.calls.append(refresh)
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]

    def drop_local(self, server, token):
        self.dropped.append(token)


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.posts = list(responses), []

    def post(self, url, headers=None, data=None, timeout=None):
        self.posts.append((url, json.loads(data)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def cv(tokens, responses, probe=lambda h, p: p == 9100):
    return Camview(tokens, "1", 10, False, session=FakeHttp(responses), probe=probe, sleep=lambda s: None)


def test_start_video_ok_drops_private_and_keeps_encoding():
    c = cv(FakeTokens(["T1"]), [Resp(200, {"code": 1000, "data": {"url": URLS, "token": "S1"}})])
    r = c.start_video("d14.camview.io", "1000796", "17")
    assert r.ok and len(r.urls) == 2
    assert all("203.0.113.7" in u and u.endswith("?token=S1") and "%24" in u for u in r.urls)
    url, payload = c._http.posts[0]
    assert payload["data"]["channelId"] == "1000796$1$0$17" and payload["data"]["streamType"] == "1"
    assert url.startswith("https://d14.camview.io/admin/API/MTS/Video/StartVideo?token=T1")


def test_401_with_urls_in_body_is_success():
    c = cv(FakeTokens(["T1"]), [Resp(401, {"data": {"url": URLS}})])
    r = c.start_video("d14.camview.io", "1", "2")
    assert r.ok and r.urls[0].endswith("?token=T1")


def test_401_rereads_token_server_never_evicts():
    tokens = FakeTokens(["OLD", "NEW"])
    c = cv(tokens, [Resp(401, {}), Resp(200, {"data": {"url": URLS, "token": "S"}})])
    r = c.start_video("d14.camview.io", "1", "2")
    assert r.ok
    assert tokens.calls == [False, True] and tokens.dropped == ["OLD"]
    assert all("/evict" not in u and "force" not in u for u, _ in c._http.posts)


def test_401_with_same_token_is_token_rejected():
    c = cv(FakeTokens(["SAME"]), [Resp(401, {})])
    r = c.start_video("d14.camview.io", "1", "2")
    assert not r.ok and r.http_status == 401 and r.detail == "token_rejected"


def test_503_2084_and_errors():
    r = cv(FakeTokens(["T"]), [Resp(503, {})]).start_video("h", "1", "2")
    assert (r.ok, r.http_status) == (False, 503)
    r = cv(FakeTokens(["T"]), [Resp(200, {"code": 2084, "data": {}})]).start_video("h", "1", "2")
    assert (r.ok, r.api_code, r.detail) == (False, 2084, "no_urls")
    r = cv(FakeTokens(["T"]), [requests.Timeout()]).start_video("h", "1", "2")
    assert r.detail == "timeout"
    r = cv(FakeTokens([None]), []).start_video("h", "1", "2")
    assert (r.stage, r.detail) == ("token", "token_unavailable")


def test_port_cache_skips_dead_ports():
    probes = []
    c = cv(FakeTokens(["T"]), [], probe=lambda h, p: probes.append(p) or p == 9100)
    urls = ["rtsp://203.0.113.7:9090/a?token=x", "rtsp://203.0.113.7:9100/a?token=x"]
    assert c.pick_url("d14", urls).startswith("rtsp://203.0.113.7:9100")
    assert probes == [9090, 9100]
    assert c.pick_url("d14", urls).startswith("rtsp://203.0.113.7:9100")
    assert probes == [9090, 9100]  # cached: no probing
    for _ in range(10):
        c.note_result("d14", False)
    assert "d14" not in c.ports()


def test_stop_video_disabled_by_default():
    c = cv(FakeTokens(["T"]), [])
    c.stop_video("h", "1", "2")
    assert c._http.posts == []


def test_helpers():
    assert is_private_ip_url("rtsp://192.168.1.4:554/x") and is_private_ip_url("rtsp://172.20.0.1/x")
    assert not is_private_ip_url("rtsp://203.0.113.7:9100/x") and not is_private_ip_url("rtsp://d14.camview.io/x")
    assert host_port("rtsp://203.0.113.7:9100/dss?x") == ("203.0.113.7", 9100)
    assert host_port("rtsp://u:p@cam.local/x") == ("cam.local", 554)


def test_token_client_caches_and_backs_off():
    class Http:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            return Resp(200, {"token": f"T{self.n}"}) if self.n == 1 else Resp(503, {"error": "auth failed"})

    t = [0.0]
    http = Http()
    tc = TokenClient("http://x", session=http, clock=lambda: t[0])
    assert tc.get("d14") == "T1" and tc.get("d14") == "T1" and http.n == 1
    t[0] = 25.0
    assert tc.get("d14") is None and http.n == 2
    t[0] = 40.0
    assert tc.get("d14") is None and http.n == 2  # failure cached 30 s
