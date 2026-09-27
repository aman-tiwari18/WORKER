# Script-Server Architecture & Scaling Strategy

## 1. High-Level Architecture
The system operates on a **Master Detection Pipeline** (`master_pipeline.py`) designed to handle thousands of CCTV feeds concurrently. It recently transitioned to a **Unified Detector (Scaling Path A)** model, deprecating the legacy approach where each analytic ran its own isolated inference model.

### Key Components:
- **Unified Detector (`unified_detector.py`)**: A single multiprocessing worker that holds one instance of the YOLO model in GPU memory. It processes batched frames from all cameras and fans out the resulting bounding boxes to lightweight, pure-logic analytic functions (e.g., intruder, crowd).
- **DSS Authentication Handler**: Uses a file-lock (`server_tokens.json`) to prevent a "thundering herd" of workers from simultaneously authenticating with the DSS server, caching tokens efficiently.
- **Round-Robin Scheduler**: Rotates a massive list of cameras through a fixed pool of capture threads, preventing the system from collapsing under the weight of holding thousands of simultaneous TCP connections open.

---

## 2. Stream Flow & Frame Capture

### RTSP Flow
1. **Fetch & Auth**: Connects to the DSS REST API (`/StartVideo`) to request secure stream tokens and private/public RTSP links.
2. **Ingestion**: Uses OpenCV `VideoCapture` explicitly configured with the FFMPEG backend. It enforces TCP transport to prevent UDP packet loss common behind NATs.
3. **Throttling**: Instead of pulling 25 FPS, the producer uses `SAMPLE_FPS=5` (or ~3 FPS in Path A). Because analytics like "Crowd" have 120s cooldowns, capturing at video speed is unnecessary.
4. **Queueing**: Raw frames are placed into a central RAM queue (`UNIFIED_QUEUE_SIZE=256`).

### HLS Flow
1. **Fetch**: Parses the `.m3u8` playlists using the DSS API.
2. **Persistent vs. Burst**: Since opening an HLS stream is extremely slow (3-10 seconds), the pipeline either holds the stream persistently or grabs a burst of 4 frames at a time (`HLS_UNIFIED_BURST_FRAMES`).
3. **Hardware Demuxing**: Uses OpenCV's `grab()` to advance the stream natively without paying the heavy CPU cost of decoding and color-converting unneeded frames, only calling `retrieve()` for the exact frames to be sent to the GPU.

---

## 3. Current Configuration Limits
- `MAX_CONCURRENT=256`: The absolute maximum number of simultaneous producer threads running per worker node.
- `MAX_STREAMS_PER_DVR=6`: A strict safety limit preventing the worker from overloading a single remote DVR.
- `GPU_BATCH_SIZE=24`: The number of frames grouped together before running a forward pass on the GPU.
- `COOLDOWNS`: Rate-limits for reporting detections to the database (e.g., Server Room: 60s, Crowd: 120s, Furniture: 300s).

---

## 4. AI Models: Strengths & Upgrades

The system currently uses **YOLOv10-M (Medium)** (`yolov10m.pt`) at a confidence threshold of **0.45**. 
- **Current Strength**: YOLOv10-M is excellent for speed because it eliminates the Non-Maximum Suppression (NMS) step. However, it can struggle with small distant objects or highly occluded subjects.
- **GPU Underutilization**: Because the bottleneck currently lies in CPU-bound network threads capturing frames, the GPU is starved for data and sits at low utilization.

### Recommended Upgrades:
Since the GPU has excess capacity, you can instantly upgrade detection reliability by swapping the model in `unified_detector.py`:
1. **YOLOv10-X (Extra Large)**: Already present in your `/models/` directory. It uses significantly more VRAM and compute, resulting in vastly better accuracy for small/occluded objects.
2. **YOLOv11**: You have `yolo11n.pt` downloaded. We highly recommend downloading **`yolo11m.pt`** or **`yolo11x.pt`**. YOLOv11 achieves higher Mean Average Precision (mAP) than YOLOv10 for the exact same computational cost.

---

## 5. Shifting the Bottleneck to RAM & GPU (Max Scaling)

Currently, the architecture is **CPU and I/O Bound** (Threads waiting on network packets and CPU software-decoding H264). To maximize the number of cameras processed per system config, we must shift the load to the underutilized RAM and GPU.

### Concrete Steps to Scale:

#### A. Enable Hardware Decoding (NVDEC)
Currently, your CPU is decoding every video frame using FFMPEG.
- **Action**: Enable `USE_NVDEC=1` in your environment.
- **Result**: The pipeline will switch to GStreamer (`nvv4l2decoder`), offloading video decompression entirely to the GPU's dedicated NVDEC silicon. This frees up massive amounts of CPU and allows you to double the number of concurrent capture threads.

#### B. Increase GPU Batch Size and RAM Buffers
If your GPU is idling, it means it is processing small batches too quickly and waiting for the CPU to fetch more frames.
- **Action**: Increase `UNIFIED_GPU_BATCH_SIZE` from `24` to `64` or `128` (depending on your GPU's VRAM size).
- **Action**: Increase `UNIFIED_GPU_QUEUE_SIZE` from `128` to `512` or `1024` to act as a massive RAM shock-absorber.
- **Result**: The CPU aggressively dumps frames into RAM. The GPU eats massive batches at once, maximizing CUDA core utilization.

#### C. Expand the Round-Robin Pool
Because you have moved decoding to the GPU and buffered with RAM, the CPU can now handle significantly more network connections.
- **Action**: Increase `RR_POOL_SIZE` from `256` to `1000+`. 
- **Result**: A single worker can monitor 1000+ cameras by rotating connections efficiently, knowing the GPU has the capacity to swallow the burst of frames.

#### D. Upgrade to YOLOv11x
Since you are shifting the bottleneck away from the CPU, you will finally have enough frames to push the GPU to 100%. 
- **Action**: Swap `yolov10m.pt` for `yolo11x.pt`.
- **Result**: Because we *want* to use the GPU to its maximum limit, running a heavier, state-of-the-art model ensures that the hardware is fully utilized while drastically cutting down on false positives and missed detections.
