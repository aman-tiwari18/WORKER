from uptime.outcomes import DOWN, UNKNOWN, UP, Attempt, map_outcome


def m(stage, **kw):
    return map_outcome(Attempt(stage, **kw))


def test_up():
    assert map_outcome(Attempt("ffmpeg", ok=True)) == (UP, "ok")


def test_infrastructure_is_unknown():
    assert m("token") == (UNKNOWN, "token_unavailable")
    assert m("startvideo", http_status=401) == (UNKNOWN, "token_rejected")
    assert m("startvideo", http_status=503) == (UNKNOWN, "dvr_unavailable")
    assert m("startvideo", detail="timeout") == (UNKNOWN, "dss_timeout")
    assert m("startvideo", detail="conn_error") == (UNKNOWN, "dss_unreachable")
    assert m("probe", detail="media_unreachable") == (UNKNOWN, "media_unreachable")
    assert m("ffmpeg", detail="unauthorized") == (UNKNOWN, "stream_token_rejected")
    assert m("portal", http_status=403, detail="blocked") == (UNKNOWN, "tcs_blocked")
    assert m("portal", detail="timeout") == (UNKNOWN, "portal_timeout")
    assert m("config", detail="missing_device_or_channel") == (UNKNOWN, "missing_device_or_channel")


def test_2084_depends_on_the_dvr_and_repeats():
    a = Attempt("startvideo", http_status=200, api_code=2084)
    assert map_outcome(a) == (UNKNOWN, "dvr_capacity")  # first, unexplained refusal
    assert map_outcome(a, dvr_recent_up=True) == (DOWN, "no_permission")
    assert map_outcome(a, repeat=True) == (DOWN, "refused_2084")  # refused again on the retry


def test_camera_side_is_down():
    assert m("startvideo", http_status=200, api_code=1105, detail="no_urls") == (DOWN, "startvideo_code_1105")
    assert m("startvideo", http_status=200, detail="no_urls") == (DOWN, "startvideo_no_urls")
    assert m("ffmpeg", detail="timeout") == (DOWN, "ffmpeg_timeout")
    assert m("ffmpeg", detail="no_frame") == (DOWN, "no_frame")
    assert m("ffmpeg", detail="no_stream") == (DOWN, "no_stream")
    assert m("ffmpeg", detail="http_404") == (DOWN, "http_404")
    assert m("portal", http_status=200, detail="hls_extract_failed") == (DOWN, "hls_extract_failed")
    assert m("portal", http_status=404, detail="portal_http_404") == (DOWN, "portal_http_404")


def test_shutdown_is_never_down():
    assert m("portal", detail="stopped") == (UNKNOWN, "stopped")
