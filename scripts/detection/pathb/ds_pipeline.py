"""DeepStream pipeline for one group of cameras that share the same model set.

    nvurisrcbin (per camera, GPU decode, optional keyframes only)
        -> nvstreammux (batch on the GPU)
        -> nvinfer model 1 [-> nvinfer model 2]   (TensorRT; one decode, both models)
        -> nvvideoconvert -> RGBA (only so a frame can be copied out when needed)
        -> fakesink, with a probe that reads the boxes

The probe hands (camera, raw boxes, frame-or-None, box space) to a callback. A
full frame is copied from the GPU only when `want_frame` says that camera is due
for evaluation; otherwise only box metadata leaves the GPU.

Streammux: the NEW nvstreammux is used by default (PATHB_NEW_STREAMMUX=1). It
batches frames at their own resolution, so nvinfer letterboxes each camera
straight from its native frame, exactly like Ultralytics does on Path A. The
legacy mux first resizes every camera to one width x height (no aspect ratio);
on the test videos that extra resize changed ~8% of person boxes vs Path A
(re-encoding a frame alone changes ~3%), and 6-7% of crowd/proximity results.
Set PATHB_NEW_STREAMMUX=0 to fall back to the legacy mux.

Requires the DeepStream SDK + pyds (DeepStream container, see deploy/deepstream/).
"""
import logging
import os
import threading

import cv2
import numpy as np

NEW_STREAMMUX = os.getenv("PATHB_NEW_STREAMMUX", "1").strip().lower() in ("1", "true", "yes")
if NEW_STREAMMUX:
    # read by the nvstreammux plugin when it loads, so it must be set before Gst.init
    os.environ["USE_NEW_NVSTREAMMUX"] = "yes"

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import pyds  # noqa: E402

log = logging.getLogger(__name__)

_loop = None
_loop_users = 0
_loop_lock = threading.Lock()


def start_main_loop():
    """One GLib main loop (default context) serves every pipeline's bus.
    Reference-counted: RTSP and HLS runners may share it in one process."""
    global _loop, _loop_users
    with _loop_lock:
        _loop_users += 1
        if _loop is None:
            Gst.init(None)
            _loop = GLib.MainLoop()
            threading.Thread(target=_loop.run, daemon=True, name="PathB-GLib").start()


def stop_main_loop():
    global _loop, _loop_users
    with _loop_lock:
        _loop_users = max(0, _loop_users - 1)
        if _loop is not None and _loop_users == 0:
            _loop.quit()
            _loop = None


def _make(factory, name, **props):
    el = Gst.ElementFactory.make(factory, name)
    if el is None:
        raise RuntimeError(f"GStreamer element '{factory}' unavailable — is DeepStream installed?")
    for k, v in props.items():
        el.set_property(k, v)
    return el


def _try_set(el, prop, value):
    """Set optional properties that differ between DeepStream versions."""
    if el.find_property(prop) is not None:
        el.set_property(prop, value)
    else:
        log.debug("[PathB] %s has no property %s", el.get_name(), prop)


class DsPipeline:
    def __init__(self, name, group, model_configs, max_sources, on_frame, want_frame,
                 on_source_error, mux_width=1920, mux_height=1080,
                 keyframes_only=True, loop_files=False, gpu_id=0):
        """group: key passed back in every callback (the model-name tuple).
        model_configs: [(model name, nvinfer config path)] in inference order."""
        self.name = name
        self.group = group
        self.max_sources = max_sources
        self.mux_width, self.mux_height = mux_width, mux_height
        self.keyframes_only = keyframes_only
        self.loop_files = loop_files
        self.gpu_id = gpu_id
        self._on_frame, self._want_frame, self._on_error = on_frame, want_frame, on_source_error
        self._sources = {}   # source id -> nvurisrcbin
        Gst.init(None)
        self._build(model_configs)

    # ---------------------------------------------------------------- build
    def _build(self, model_configs):
        p = self.pipeline = Gst.Pipeline.new(f"pathb-{self.name}")
        self.mux = _make("nvstreammux", "mux", **{"batch-size": self.max_sources})
        _try_set(self.mux, "batched-push-timeout", 40000)
        if not NEW_STREAMMUX:
            # legacy mux scales every camera to one size; CUDA unified memory (3)
            # so the probe can map a frame on dGPU when needed
            for prop, value in (("width", self.mux_width), ("height", self.mux_height),
                                ("live-source", 1), ("gpu-id", self.gpu_id), ("nvbuf-memory-type", 3)):
                _try_set(self.mux, prop, value)
        chain = [self.mux]
        for model_name, cfg_path in model_configs:
            chain.append(_make("nvinfer", f"infer-{model_name}", **{
                "config-file-path": cfg_path, "gpu-id": self.gpu_id}))
        conv = _make("nvvideoconvert", "to-rgba", **{"gpu-id": self.gpu_id, "nvbuf-memory-type": 3})
        caps = _make("capsfilter", "rgba", caps=Gst.Caps.from_string("video/x-raw(memory:NVMM), format=RGBA"))
        sink = _make("fakesink", "sink", **{"sync": False, "async": False, "enable-last-sample": False})
        chain += [conv, caps, sink]
        for el in chain:
            p.add(el)
        for a, b in zip(chain, chain[1:]):
            if not a.link(b):
                raise RuntimeError(f"cannot link {a.get_name()} -> {b.get_name()}")
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._probe)
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"pipeline {self.name} failed to start")
        log.info("[PathB] pipeline %s playing (max %d sources)", self.name, self.max_sources)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    # -------------------------------------------------------------- sources
    def add_source(self, sid, uri):
        """Attach camera `sid` (0..max_sources-1). Runs on the GLib loop."""
        GLib.idle_add(self._add, sid, uri)

    def remove_source(self, sid):
        GLib.idle_add(self._remove, sid)

    def _add(self, sid, uri):
        if sid in self._sources:
            self._remove(sid)
        src = _make("nvurisrcbin", f"src-{sid}", uri=uri)
        _try_set(src, "gpu-id", self.gpu_id)
        _try_set(src, "dec-skip-frames", 2 if self.keyframes_only else 0)  # 2 = decode keyframes only
        _try_set(src, "select-rtp-protocol", 4)                            # RTSP over TCP
        _try_set(src, "rtsp-reconnect-interval", 0)                        # the stream manager reconnects
        _try_set(src, "latency", 200)
        if self.loop_files and uri.startswith("file://"):
            _try_set(src, "file-loop", True)
        src.connect("pad-added", self._on_pad_added, sid)
        self.pipeline.add(src)
        src.sync_state_with_parent()
        self._sources[sid] = src
        return False  # one-shot idle callback

    def _on_pad_added(self, src, pad, sid):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps.get_structure(0).get_name().startswith("video"):
            return
        sinkpad = self.mux.request_pad_simple(f"sink_{sid}")
        if pad.link(sinkpad) != Gst.PadLinkReturn.OK:
            log.error("[PathB] %s: could not link source %d", self.name, sid)

    def _remove(self, sid):
        src = self._sources.pop(sid, None)
        if src is None:
            return False
        src.set_state(Gst.State.NULL)
        sinkpad = self.mux.get_static_pad(f"sink_{sid}")
        if sinkpad is not None:
            sinkpad.send_event(Gst.Event.new_flush_stop(False))
            self.mux.release_request_pad(sinkpad)
        self.pipeline.remove(src)
        return False

    # ---------------------------------------------------------------- probe
    def _probe(self, pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        batch = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        l_frame = batch.frame_meta_list
        while l_frame is not None:
            fm = pyds.NvDsFrameMeta.cast(l_frame.data)
            sid = fm.pad_index
            raw = []
            l_obj = fm.obj_meta_list
            while l_obj is not None:
                om = pyds.NvDsObjectMeta.cast(l_obj.data)
                r = om.rect_params
                raw.append((om.unique_component_id, om.class_id, om.confidence,
                            r.left, r.top, r.width, r.height))
                l_obj = l_obj.next
            # coordinate space of rect_params: the camera's own frame (new mux)
            # or the legacy mux's output size
            if NEW_STREAMMUX:
                space = (fm.source_frame_width, fm.source_frame_height)
            else:
                space = (self.mux_width, self.mux_height)
            frame = None
            if self._want_frame(self.group, sid):
                surface = pyds.get_nvds_buf_surface(hash(buf), fm.batch_id)
                frame = cv2.cvtColor(np.array(surface, copy=True, order="C"), cv2.COLOR_RGBA2BGR)
            try:
                self._on_frame(self.group, sid, raw, frame, space=space)
            except Exception as exc:  # never let a callback error stall the stream
                log.error("[PathB] on_frame failed for %s/%d: %s", self.name, sid, exc)
            l_frame = l_frame.next
        return Gst.PadProbeReturn.OK

    # ------------------------------------------------------------------ bus
    def _sid_of(self, msg):
        path = msg.src.get_path_string() if msg.src else ""
        for part in path.split("/"):
            if part.startswith("src-") and part[4:].isdigit():
                return int(part[4:])
        return None

    def _on_bus(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            sid = self._sid_of(msg)
            if sid is None:
                log.error("[PathB] %s pipeline error: %s (%s)", self.name, err, dbg)
            else:
                self._on_error(self.group, sid, str(err))
        elif msg.type == Gst.MessageType.ELEMENT and pyds.gst_nvmessage_is_stream_eos(msg):
            sid = pyds.gst_nvmessage_parse_stream_eos(msg)
            self._on_error(self.group, sid, "end of stream")
        elif msg.type == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning()
            log.debug("[PathB] %s warning: %s", self.name, w)
        return True
