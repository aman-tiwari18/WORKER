# Scaling Path B: DeepStream + TensorRT

## Overview
While Path A (GStreamer + PyTorch) scales the system to ~800 cameras per GPU by moving decoding to hardware, **Scaling Path B** completely redesigns the pipeline into an industrial-grade, single-process C++ architecture using NVIDIA DeepStream. This pushes the theoretical limit up to **3,000 - 4,000 cameras per H200 GPU**.

---

## 1. How Path B Solves Your Current Bottlenecks

### A. Eliminating Thread Overload (Single-Process Architecture)
Currently, you use Python `multiprocessing` and threads to manage RTSP/HLS streams. This causes massive CPU context-switching, thread lockups, and 100% CPU utilization.
* **The Path B Solution**: DeepStream eliminates Python multi-threading for stream capture. It uses a single C++ element (`nvurisrcbin`) to manage thousands of RTSP and HLS connections concurrently inside **one single process**. 
* **The Result**: Thread overload drops to zero. You no longer need to manage complex Python thread pools.

### B. Eliminating "Revisit Latency" (Persistent Streams)
You mentioned that HLS has a 10-minute revisit latency because the slot window is too small, and RTSP still suffers from rotation delays.
* **The Path B Solution**: Because DeepStream is so efficient, you no longer need a small "Round-Robin" pool. You can connect to **3,000 cameras simultaneously** and hold the connections open persistently. 
* **The Result**: Revisit latency drops to **0 seconds**. Every camera is monitored constantly, allowing you to catch split-second events (like a cell phone appearing) without missing them during a 10-minute rotation blind spot.

### C. Fixing 1-3% GPU Utilization (VRAM Batching)
Your GPU is currently starving at 1-3% because the CPU cannot push frames fast enough across the PCIe bus into GPU RAM (Host-to-Device transfer bottleneck).
* **The Path B Solution**: DeepStream uses `nvstreammux`. The video stream enters the network card, is hardware-decoded by NVDEC directly into GPU VRAM, and is batched into a single tensor without **ever touching the host CPU or system RAM**.
* **The Result**: Your GPU will immediately spike to 80-100% utilization, finally justifying the hardware cost by processing thousands of frames per second.

---

## 2. Upgrading the AI Model (TensorRT)

In Path A, you run the raw PyTorch model (`yolov10m.pt`). PyTorch is flexible but slow for production.
* **The Path B Solution**: We do **not** retrain the model. Instead, we compile it using TensorRT. 
  `yolov10m.pt` ➔ `yolov10m.onnx` ➔ `yolov10m_h200_fp16.engine`
* **Why this matters**: A TensorRT `.engine` file runs **3x to 5x faster** than a `.pt` file on the exact same GPU because it fuses layers and drops precision to FP16 (with negligible accuracy loss).
* **Stronger Models**: Because TensorRT makes inference 5x faster, you can seamlessly upgrade to **YOLOv10-X** or **YOLOv11-X** (which are much stronger and more reliable) and they will run at the *same speed* that your current weak model runs today.

---

## 3. The Path B Architecture Flow

```text
    [ thousands of RTSP/HLS URLs ]
                 │
                 ▼
    [ nvurisrcbin ] (Hardware Decode NVDEC)
                 │  * Frames stay in GPU Memory
                 ▼
    [ nvstreammux ] (Batches 64-128 frames instantly)
                 │
                 ▼
    [ nvinfer ] (YOLOv10x TensorRT Engine) 
                 │  * 1 Inference for ALL rules
                 ▼
    [ Probe Callback ] (Python logic: Crowd, Intruder, Server Room)
                 │
                 ▼
    [ MySQL DB & Async Snapshot Save ]
```

---

## 4. Expected System Improvements
| Metric | Current State | Path A (GStreamer) | Path B (DeepStream) |
| :--- | :--- | :--- | :--- |
| **Max Cameras per GPU** | ~100-256 | ~800 | **3,000 - 4,000** |
| **HLS Latency** | ~10 Minutes | ~5 Seconds | **0 Seconds (Persistent)** |
| **Thread Overload** | Critical (Crash risk) | Moderate | **None (C++ Pipeline)** |
| **GPU Utilization** | 1 - 3% | ~40 - 60% | **80 - 100%** |
| **Model Format** | PyTorch (`.pt`) | PyTorch (`.pt`) | **TensorRT (`.engine`)** |

## Summary of Next Steps
If you choose Path B, the immediate development steps are:
1. Compile your current `yolov10m.pt` (or download `yolov11x.pt` for stronger detections) into a TensorRT `.engine` file.
2. Spin up the NVIDIA DeepStream Docker container (`nvcr.io/nvidia/deepstream`).
3. Replace the Python `master_pipeline.py` multi-processing logic with a DeepStream Python bindings (`pyds`) graph using `nvurisrcbin`.
