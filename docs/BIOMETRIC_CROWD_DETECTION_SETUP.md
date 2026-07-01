# Biometric Crowd Detection - Quick Setup Guide

## Overview
Separate crowd detection implementation specifically for Biometric area cameras, independent from Entry/Exit crowd detection.

## New Files Created

### 1. Core Detection Module
**File:** `script-server/scripts/detection/Crowd_det_biometric.py`

**Key Functions:**
- `get_yolo_model()` - Loads and returns YOLO model
- `detect_crowd_biometric_in_frame(frame, device_id, camera_no, script_name)` - Single frame detection
- `detect_crowd_biometric_batch(items)` - Batch inference for multiple frames

**Configuration:**
```python
COOLDOWN_TIME = 240       # Seconds between saves (4 minutes)
MIN_PERSONS = 12          # Threshold for crowd violation
CONF_THRESHOLD = 0.45     # Confidence for person detection
SAVE_FOLDER = "results/crowd_detection_biometric"
```

### 2. Worker Module
**File:** `script-server/scripts/detection/workers/crowd_det_biometric_worker.py`

**Key Functions:**
- `crowd_det_biometric_worker(frame_queue)` - RTSP worker entry point
- `crowd_det_biometric_worker_hls(camera_configs, stop_event=None)` - HLS worker entry point

**Features:**
- GPU batch processing (batch size: 64)
- Automatic camera filtering by classification
- Supports both RTSP and HLS streams
- Database reporting integration

## Camera Classification

**Required Classification Value:** `biometric`

**Supported Variations:**
- `biometric`
- `Biometric`
- `BIOMETRIC`

The filtering is case-insensitive.

## Usage Examples

### 1. HLS Standalone Execution

```python
from workers.crowd_det_biometric_worker import crowd_det_biometric_worker_hls
import threading

# Camera configurations
cameras = [
    {
        'camera_id': 'CAM001',
        'ch_no': '1',
        'classification': 'biometric',
        'cctv_url': 'https://example.com/stream.m3u8',
        'exam_id': '12345',
        'script_code': 'crowd_biometric'
    },
    # ... more cameras
]

# Create stop event
stop_event = threading.Event()

# Start worker
crowd_det_biometric_worker_hls(cameras, stop_event)

# To stop
stop_event.set()
```

### 2. RTSP with Master Pipeline

```python
from workers.crowd_det_biometric_worker import crowd_det_biometric_worker
import queue

# Create frame queue
frame_queue = queue.Queue()

# Start worker in thread
import threading
worker_thread = threading.Thread(
    target=crowd_det_biometric_worker,
    args=(frame_queue,),
    daemon=True
)
worker_thread.start()

# Feed frames
frame_queue.put({
    'frame': frame,
    'device_id': 'DEV001',
    'camera_no': '1',
    'exam_id': '12345'
})

# Stop worker
frame_queue.put(None)
worker_thread.join()
```

## Output Structure

### Saved Images
**Location:** `script-server/scripts/detection/results/crowd_detection_biometric/`

**Naming Convention:**
- HLS: `crowd_biometric_ch{ch_no}_exam{exam_id}_{timestamp}_{uuid}.jpg`
- RTSP: `crowd_biometric_{device_id}_{camera_no}_exam{exam_id}_{timestamp}_{uuid}.jpg`

**Example:**
```
crowd_biometric_ch1_exam12345_20260502_143022_a1b2c3d4.jpg
crowd_biometric_DEV001_1_exam12345_20260502_143022_e5f6g7h8.jpg
```

### Database Reporting

**HLS Cameras:**
```python
report_to_db_hls(
    ch_code=str(ch_no),
    image_name=fname,
    folder="crowd_detection_biometric"
)
```

**RTSP Cameras:**
```python
report_to_db_rtsp(
    device_id=str(dev_id),
    camera_no=str(ch_no),
    image_name=fname,
    folder="crowd_detection_biometric"
)
```

## Log Messages

**Startup:**
```
🚀 [CrowdBiometric] GPU worker started (true batch mode)
🟢 [CrowdBiometric] RTSP worker started
```

**Detection:**
```
[CrowdBiometric] 🚨 SAVED persons=15 → /path/to/image.jpg
```

**Errors:**
```
[CrowdBiometric] Batch inference error: {error_message}
[CrowdBiometric] HLS DB Reporting Error: {error_message}
[CrowdBiometric] RTSP DB Reporting Error: {error_message}
```

**Shutdown:**
```
🔴 [CrowdBiometric] GPU worker stopped
```

## Differences from Entry/Exit Crowd Detection

| Feature | Entry/Exit (`crowd_worker.py`) | Biometric (`crowd_det_biometric_worker.py`) |
|---------|-------------------------------|---------------------------------------------|
| Core Module | `Crowd_det_complete.py` | `Crowd_det_biometric.py` |
| Classification Filter | `entry/exit`, `entry_exit`, `entryexit` | `biometric` |
| Save Folder | `results/crowd_detection` | `results/crowd_detection_biometric` |
| DB Folder | `crowd_detection` | `crowd_detection_biometric` |
| Script Name | `crowd` | `crowd_biometric` |
| Log Prefix | `[Crowd]` | `[CrowdBiometric]` |

## Integration Checklist

- [ ] Ensure `Crowd_det_biometric.py` is in `script-server/scripts/detection/`
- [ ] Ensure `crowd_det_biometric_worker.py` is in `script-server/scripts/detection/workers/`
- [ ] Verify YOLO model exists at `script-server/scripts/detection/models/yolov10m.pt`
- [ ] Create output directories (auto-created on first run):
  - `script-server/scripts/detection/results/crowd_detection_biometric/`
  - `script-server/scripts/detection/results/tcs_detections/crowd_detection_biometric/`
- [ ] Update database schema to support `crowd_detection_biometric` folder
- [ ] Update master pipeline to register biometric worker (if using RTSP)
- [ ] Set camera `classification` field to `biometric` in database
- [ ] Test with sample biometric camera streams
- [ ] Verify database reporting works correctly
- [ ] Monitor logs for proper filtering and detection

## Troubleshooting

### No cameras being processed
**Check:**
1. Camera `classification` field contains `biometric` (case-insensitive)
2. Camera configs are being passed to the worker
3. HLS URLs are valid and accessible
4. Check logs for "Scene filtering" messages

### Images not saving
**Check:**
1. Person count >= 12 (MIN_PERSONS threshold)
2. Cooldown time (240 seconds) has elapsed since last save
3. Output directory has write permissions
4. Disk space available

### Database reporting fails
**Check:**
1. `utils.db_client` module is accessible
2. Database connection is active
3. `crowd_detection_biometric` folder exists in database schema
4. Check error logs for specific database errors

### Model loading issues
**Check:**
1. YOLO model file exists at `models/yolov10m.pt`
2. CUDA is available if using GPU
3. Sufficient GPU memory
4. PyTorch and Ultralytics packages installed

## Performance Tuning

### Adjust Detection Threshold
Edit `Crowd_det_biometric.py`:
```python
MIN_PERSONS = 10  # Lower threshold for more sensitive detection
```

### Adjust Cooldown Time
Edit `Crowd_det_biometric.py`:
```python
COOLDOWN_TIME = 300  # 5 minutes between saves
```

### Adjust Batch Size
Edit `crowd_det_biometric_worker.py`:
```python
GPU_BATCH_SIZE = 32  # Smaller batch for less GPU memory usage
```

### Adjust Sampling Rate (HLS)
Edit `crowd_det_biometric_worker.py`:
```python
HLS_SAMPLE_EVERY = 10  # Process every 10th frame instead of every 5th
```

## Monitoring

### Key Metrics to Monitor
1. **Detection Rate:** Number of crowd detections per hour
2. **False Positives:** Detections with person count just above threshold
3. **Processing Latency:** Time between frame capture and detection
4. **GPU Utilization:** Monitor CUDA memory usage
5. **Queue Depth:** Monitor `_gpu_queue` size

### Recommended Monitoring Commands
```bash
# Watch log file
tail -f /path/to/logs | grep CrowdBiometric

# Count detections
grep "CrowdBiometric.*SAVED" /path/to/logs | wc -l

# Check GPU usage
nvidia-smi -l 1

# Monitor output folder
watch -n 5 'ls -lh results/crowd_detection_biometric/ | tail -20'
```
