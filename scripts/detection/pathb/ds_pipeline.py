"""DeepStream pipeline for one group of cameras that share the same model set.

    nvurisrcbin (per camera, GPU decode, optional keyframes only)
        -> nvstreammux (batch on the GPU)
        -> nvinfer model 1 [-> nvinfer model 2]   (TensorRT; one decode, both models)
        -> nvvideoconvert -> RGBA (only so a frame can be copied out when needed)
        -> fakesink, with a probe that reads the boxes

The probe hands (camera, raw boxes, frame-or-None, box space) to a callback. A
full frame is copied from the GPU only when `want_frame` says that camera is due
for evaluation; otherwise only box metadata leaves the GPU.

Streammux: the classic nvstreammux, which scales every camera to one fixed
mux_width x mux_height (default 1920x1080), so box coordinates are always in a
known space. The new nvstreammux (USE_NEW_NVSTREAMMUX) was tried and dropped:
tested on DeepStream 7.1, nvurisrcbin's converter scales every source to the
resolution of whichever camera attached FIRST, which changes as cameras rotate.
The frame handed to the rules is resized back to the camera's own resolution
(boxes are scaled to it in to_detections), so rules and saved evidence keep the
camera's aspect ratio. 1920x1080 cameras (most DSS channels) are never resized.

Requires the DeepStream SDK + pyds (DeepStream container, see deploy/deepstream/).
"""
import ctypes
import logging
import os
import threading

import cv2
import numpy as np

# GStreamer's RTSP/HTTP sources ask GIO for proxy settings; GIO's libproxy backend
# throws std::runtime_error("Unable to read configuration") inside the container and
# aborts the whole process on the first live stream. The workers use no proxy.
os.environ.setdefault("GIO_USE_PROXY_RESOLVER", "dummy")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

import pyds  # noqa: E402

log = logging.getLogger(__name__)

_BUS_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parsers", "libpathb_bus.so")


def _drop_qos_messages(bus):
    """Drop decoder QoS messages before they reach the bus queue (see parsers/pathb_bus.c).

    Keyframes-only decoding makes every skipped frame post a QoS message (~24 per
    camera per second); left alone they jam the bus and stall the pipeline. Uses the
    C filter so no Python runs per message; falls back to a Python sync handler."""
    try:
        lib = ctypes.CDLL(_BUS_LIB)
        get = ctypes.pythonapi.PyCapsule_GetPointer
        get.restype, get.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
        lib.pathb_drop_qos_messages.argtypes = [ctypes.c_void_p]
        lib.pathb_drop_qos_messages(get(bus.__gpointer__, None))
    except (OSError, AttributeError) as exc:
        log.warning("[PathB] %s unavailable (%s); dropping QoS messages in Python", _BUS_LIB, exc)
        bus.set_sync_handler(lambda b, m: Gst.BusSyncReply.DROP
                             if m.type == Gst.MessageType.QOS else Gst.BusSyncReply.PASS)

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


FDS_PER_SOURCE = 16   # measured ~11.5 open files per nvurisrcbin (decoder, sockets, polls)


def raise_fd_limit(needed):
    """Raise this process's open-file soft limit to its hard limit.

    Every source holds ~12 file descriptors, and during rotation the old decoder
    still holds its fds while the new one opens. At Docker's default soft limit
    (1024) ~80 sources exhaust it: new decoders fail with "Failed to allocate
    required memory" or the pipeline silently stops. Returns (soft, hard)."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = hard if hard != resource.RLIM_INFINITY else max(soft, needed, 65536)
    if soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        soft = want
    if soft < needed:
        log.warning("[PathB] open-file limit %d is below the ~%d needed; raise the container's "
                    "nofile ulimit or lower PATHB_MAX_SOURCES_PER_PIPELINE", soft, needed)
    return soft, hard


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
        self._stopping = False
        Gst.init(None)
        self._build(model_configs)

    # ---------------------------------------------------------------- build
    def _build(self, model_configs):
        p = self.pipeline = Gst.Pipeline.new(f"pathb-{self.name}")
        # CUDA unified memory (3) so the probe can map a frame on dGPU when needed.
        self.mux = _make("nvstreammux", "mux", **{
            "batch-size": self.max_sources, "width": self.mux_width, "height": self.mux_height,
            "live-source": 1, "batched-push-timeout": 40000, "gpu-id": self.gpu_id,
            "nvbuf-memory-type": 3})
        # Without this the mux ends the whole pipeline (EOS) the moment every source
        # is gone — e.g. all cameras rotating out in one tick, or a network outage —
        # and no source added afterwards ever produces a frame again.
        _try_set(self.mux, "drop-pipeline-eos", True)
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
        _drop_qos_messages(bus)
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"pipeline {self.name} failed to start")
        log.info("[PathB] pipeline %s playing (max %d sources)", self.name, self.max_sources)

    def stop(self, timeout=None):
        """Stop everything in one go: setting the pipeline to NULL takes every source
        down with it. Runs ON the GLib thread, like _add/_remove, so it can never
        overlap a source add/remove — under overload (cameras being dropped at the
        moment of shutdown) the two raced for the state locks and deadlocked."""
        self._stopping = True
        done = threading.Event()

        def _do():
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self._sources.clear()
            done.set()
            return False

        # tearing down a source takes ~0.1-0.2 s; 300 sources took over 20 s
        timeout = timeout or max(20.0, 0.25 * len(self._sources))
        GLib.idle_add(_do)
        if not done.wait(timeout):
            log.error("[PathB] pipeline %s did not stop within %.0fs", self.name, timeout)

    # -------------------------------------------------------------- sources
    def add_source(self, sid, uri):
        """Attach camera `sid` (0..max_sources-1). Runs on the GLib loop."""
        GLib.idle_add(self._add, sid, uri)

    def remove_source(self, sid):
        GLib.idle_add(self._remove, sid)

    def _add(self, sid, uri):
        if self._stopping:
            return False
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
        if self._stopping:
            return False
        src = self._sources.pop(sid, None)
        if src is None:
            return False
        if src.set_state(Gst.State.NULL) == Gst.StateChangeReturn.ASYNC:
            src.get_state(5 * Gst.SECOND)   # the decoder must be really stopped before its pad goes
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
            # rect_params are in the mux's output space
            space = (self.mux_width, self.mux_height)
            frame = None
            if self._want_frame(self.group, sid):
                surface = pyds.get_nvds_buf_surface(hash(buf), fm.batch_id)
                try:
                    frame = cv2.cvtColor(np.array(surface, copy=True, order="C"), cv2.COLOR_RGBA2BGR)
                finally:
                    # every map must be undone on dGPU, or mapped surfaces pile up until
                    # the buffer pool runs dry and the whole pipeline stalls (seen at
                    # ~50 copies/s: frames fell from 55/s to 1/s within a minute)
                    del surface
                    pyds.unmap_nvds_buf_surface(hash(buf), fm.batch_id)
                w, h = fm.source_frame_width, fm.source_frame_height
                if w > 0 and h > 0 and (w, h) != (frame.shape[1], frame.shape[0]):
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)  # camera's own size
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
        elif msg.type == Gst.MessageType.ELEMENT:
            # nvstreammux posts "stream-eos" with the source's pad index. Read the
            # structure directly: pyds 1.2.0 has no gst_nvmessage_* bindings.
            s = msg.get_structure()
            if s is not None and s.get_name() == "stream-eos":
                ok, sid = s.get_uint("stream-id")
                if not ok:
                    ok, sid = s.get_int("stream-id")
                if ok:
                    self._on_error(self.group, sid, "end of stream")
        elif msg.type == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning()
            log.debug("[PathB] %s warning: %s", self.name, w)
        return True
