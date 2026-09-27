"""End-to-end run_pathb with DeepStream replaced by a fake pipeline: real manifest,
nvinfer config rendering, stream manager, evaluator (dry run) and analytics rules."""
import os
import sys
import tempfile
import types

import numpy as np

from pathb import runner


class FakeDsPipeline:
    instances = []

    def __init__(self, name, group, model_configs, max_sources, on_frame, want_frame,
                 on_source_error, **kw):
        self.name, self.group, self.model_configs = name, group, model_configs
        self.on_frame, self.want_frame = on_frame, want_frame
        self.sources = {}
        FakeDsPipeline.instances.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def add_source(self, sid, uri):
        self.sources[sid] = uri

    def remove_source(self, sid):
        self.sources.pop(sid, None)

    def emit(self):
        """One frame per active source: two people standing close (proximity)."""
        frame = np.full((1080, 1920, 3), 90, np.uint8)
        raw = [(1, 0, 0.92, 400, 300, 160, 400), (1, 0, 0.90, 520, 310, 170, 410)]
        for sid in list(self.sources):
            f = frame.copy() if self.want_frame(self.group, sid) else None
            self.on_frame(self.group, sid, raw, f, space=(1920, 1080))


def _install_fake_deepstream():
    mod = types.ModuleType("pathb.ds_pipeline")
    mod.DsPipeline = FakeDsPipeline
    mod.start_main_loop = lambda: None
    mod.stop_main_loop = lambda: None
    sys.modules["pathb.ds_pipeline"] = mod


def test_split_and_collapse():
    cams = [
        {"camera_id": "100", "ch_no": "1", "script_code": "proximity"},
        {"camera_id": "100", "ch_no": "1", "script_code": "phone"},
        {"camera_id": "100", "ch_no": "1", "script_code": "tamper"},      # classical CV
        {"camera_id": "200", "ch_no": "3", "script_code": "crowd"},
    ]
    pb, rest = runner.split_for_pathb(cams)
    assert [c["script_code"] for c in rest] == ["tamper"]
    cfgs = runner.collapse(pb, "RTSP")
    assert [c["enabled_scripts"] for c in cfgs] == [["proximity", "phone"], ["crowd"]]
    keys = runner.choose_persistent(cfgs, "RTSP", capacity=1, persistent_analytics={"phone"}, share=1.0)
    assert keys == {"100_1"}


def test_run_pathb_end_to_end():
    _install_fake_deepstream()
    FakeDsPipeline.instances.clear()
    out = tempfile.mkdtemp(prefix="pathb_dry_")
    env = {"PATHB_DRY_RUN_DIR": out, "PATHB_EVAL_INTERVAL_S": "0", "PATHB_PROGRESS_EVERY": "0",
           "PATHB_MAX_SOURCES_PER_PIPELINE": "4"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        cams = [{"camera_id": f"dev{i}", "ch_no": "1", "script_code": "proximity",
                 "cctv_url": f"file:///videos/cam{i}.mp4", "exam_id": "1", "shift_id": "1"}
                for i in range(6)]
        ticks = {"n": 0}

        def should_stop():
            ticks["n"] += 1
            for p in FakeDsPipeline.instances:
                p.emit()
            return ticks["n"] > 4

        runner.run_pathb(cams, "RTSP", helpers=None, should_stop=should_stop)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert len(FakeDsPipeline.instances) == 1
    p = FakeDsPipeline.instances[0]
    assert p.group == ("yolo26",) and p.model_configs[0][0] == "yolo26"
    cfg_text = open(p.model_configs[0][1]).read()
    assert "{" not in cfg_text and "batch-size=4" in cfg_text and "pre-cluster-threshold=0.3" in cfg_text
    hits = [f for f in os.listdir(out) if f.startswith("proximity_")]
    assert hits, "expected proximity hits saved by the dry run"
    return f"{len(hits)} annotated hit(s) in {out}"
