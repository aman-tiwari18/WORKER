from uptime.hls import TokenBucket, extract_m3u8, is_block


def test_extract_methods():
    assert extract_m3u8('<a href="https://cdn.x/live/a.m3u8?t=1">') == "https://cdn.x/live/a.m3u8?t=1"
    assert extract_m3u8("{}", {"stream_url": "https://cdn.x/b.m3u8"}) == "https://cdn.x/b.m3u8"
    assert extract_m3u8("no stream here") is None


def test_block_detection():
    assert is_block(400, "403 Forbidden ... Client IP 161.248.106.76 ... Reference #18.abc")
    assert not is_block(404, "Client IP")
    assert not is_block(403, "token expired")


def test_bucket_paces_requests():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    b = TokenBucket(0.5, clock=lambda: t[0], sleep=sleep)
    assert b.take(1)  # starts full (1 token)
    assert b.take(2)  # bigger than capacity: waits until full, then goes into debt
    assert t[0] == 2.0
    b.take(1)
    assert t[0] >= 6.0  # debt of 1 + 1 more = 4 s at 0.5/s
