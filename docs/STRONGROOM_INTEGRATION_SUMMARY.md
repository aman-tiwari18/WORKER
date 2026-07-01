# Strong Room Movement Detection - Integration Summary

## ✅ Integration Complete

The Strong Room Movement detection script has been fully integrated into the master pipeline and all related systems.

---

## 📁 Files Created

### 1. **Detection Scripts**
- ✅ `script-server/scripts/detection/StrongRoom_det.py`
  - RTSP/DAV video detection script
  - Uses YOLO for person detection
  - Green bounding boxes (different from intruder's red)
  - Scene filtering for strong rooms, vaults, secure rooms
  - Saves to: `results/strongroom_detection/`
  - Filename format: `strongroom_{device_id}_{camera_no}_{timestamp}.jpg`

### 2. **Worker Scripts**
- ✅ `script-server/scripts/detection/workers/strongroom_worker.py`
  - Supports both RTSP and HLS streams
  - GPU batch queue architecture for HLS
  - Multiprocessing queue for RTSP
  - Scene filtering: 'strong room', 'strongroom', 'vault', 'secure room', 'treasury'
  - Separate save folders:
    - RTSP: `results/strongroom_detection/`
    - HLS: `results/tcs_detections/strongroom_detection/`
  - Filename formats:
    - HLS: `strongroom_ch{camera_no}_{timestamp}.jpg`
    - RTSP: `strongroom_{device_id}_{camera_no}_{timestamp}.jpg`
  - DB reporting integration via `db_client.py`
  - Graceful shutdown with Ctrl+C support

---

## 🔧 Integration Points

### 1. **Master Pipeline** (`script-server/scripts/detection/pipeline/master_pipeline.py`)
✅ Added strongroom worker mapping in `get_worker()` function:
```python
"strongroom": ("strongroom_worker", "strongroom_worker", "strongroom_worker_hls"),
```

✅ Added to graceful shutdown handler:
```python
"workers.strongroom_worker",
```

### 2. **Pipeline Manager** (`script-server/scripts/detection/backend/pipeline_manager.py`)
✅ Added to `ALL_ANALYTICS` list:
```python
ALL_ANALYTICS = [
    # ... other analytics ...
    "strongroom",
]
```

### 3. **Main API** (`script-server/app/main.py`)
✅ Added to `DETECTION_FOLDERS`:
```python
DETECTION_FOLDERS = {
    # ... other folders ...
    "strongroom": "strongroom_detection",
}
```

✅ Added to `DETECTION_LABELS`:
```python
DETECTION_LABELS = {
    # ... other labels ...
    "strongroom": "Strong Room Movement",
}
```

### 4. **Result Directories**
✅ Created directories:
- `script-server/scripts/detection/results/strongroom_detection/` (RTSP)
- `script-server/scripts/detection/results/tcs_detections/strongroom_detection/` (HLS)

---

## 🚀 Usage

### Start Strong Room Detection

#### Via API (Manual Start):
```bash
curl -X POST http://localhost:8000/cctv_dev/analytics/start \
  -H "Content-Type: application/json" \
  -d '{"analytics": ["strongroom"]}'
```

#### Via Script Execution (Scheduled):
The detection will automatically start when:
1. An exam is scheduled with strongroom detection script assigned
2. The script-server polls and finds pending executions
3. Cameras with classification matching strong room scenes are processed

#### Via Master Pipeline (Direct):
```bash
cd script-server/scripts/detection/pipeline
python3 master_pipeline.py --analytics strongroom
```

### Stop Strong Room Detection
```bash
curl -X POST http://localhost:8000/cctv_dev/analytics/stop \
  -H "Content-Type: application/json" \
  -d '{"analytics": ["strongroom"]}'
```

### Check Status
```bash
curl http://localhost:8000/cctv_dev/analytics/status
```

---

## 🎯 Detection Features

### Scene Filtering
The worker automatically filters cameras based on classification:
- ✅ 'strong room'
- ✅ 'strongroom'
- ✅ 'vault'
- ✅ 'secure room'
- ✅ 'treasury'

### Detection Logic
- **Trigger**: Any person detected (MIN_PERSONS = 0)
- **Confidence**: 0.30 threshold
- **Cooldown**: 0 seconds (captures every frame with movement)
- **Visual**: Green bounding boxes around detected persons
- **Model**: YOLOv10m

### Database Integration
- Reports detections to `camera_report_hls` (HLS) or `camera_report_rtsp` (RTSP)
- Includes metadata: camera_id, camera_name, center_name, detection_type, etc.
- Supports `pushed_to_client` flag for client dashboard filtering

---

## 📊 API Endpoints

### List Strong Room Detections
```bash
curl "http://localhost:8000/cctv_dev/detections?type=strongroom&page=1&limit=50"
```

### Filter by Date
```bash
curl "http://localhost:8000/cctv_dev/detections?type=strongroom&date=2026-04-27"
```

### Filter by Exam
```bash
curl "http://localhost:8000/cctv_dev/detections?type=strongroom&exam_id=123"
```

### Serve Detection Images
- HLS: `GET /cctv_dev/serve-detection-images/strongroom_detection/{filename}`
- RTSP: `GET /cctv_dev/serve-rtsp-detection-images/strongroom_detection/{filename}`

---

## 🔍 Architecture

### RTSP Flow
```
RTSP Producer → Multiprocessing Queue → strongroom_worker()
                                       ↓
                                    YOLO Detection
                                       ↓
                                  Save to RTSP folder
                                       ↓
                                  Report to DB
```

### HLS Flow
```
HLS Camera Configs → strongroom_worker_hls()
                            ↓
                    Camera Threads (ThreadPoolExecutor)
                            ↓
                    GPU Batch Queue (_gpu_queue)
                            ↓
                    GPU Batch Worker (batched YOLO)
                            ↓
                    Save to HLS folder
                            ↓
                    Report to DB
```

---

## ✨ Key Differences from Intruder Detection

| Feature | Intruder Detection | Strong Room Detection |
|---------|-------------------|----------------------|
| **Bounding Box Color** | Red (255, 0, 0) | Green (0, 255, 0) |
| **Scene Filter** | Outdoor areas | Indoor secure areas |
| **Purpose** | Unauthorized entry | Movement monitoring |
| **Folder Name** | `intruder_detection` | `strongroom_detection` |
| **Label** | "Intruder Detection" | "Strong Room Movement" |

---

## 🧪 Testing

### Test RTSP Detection
```bash
cd script-server/scripts/detection
python3 StrongRoom_det.py /path/to/test/video.dav
```

### Test HLS Worker
```bash
cd script-server/scripts/detection/pipeline
python3 master_pipeline.py --analytics strongroom
```

### Verify Integration
```bash
# Check if strongroom is in ALL_ANALYTICS
curl http://localhost:8000/cctv_dev/analytics/status

# Start strongroom detection
curl -X POST http://localhost:8000/cctv_dev/analytics/start \
  -H "Content-Type: application/json" \
  -d '{"analytics": ["strongroom"]}'

# Check running status
curl http://localhost:8000/cctv_dev/analytics/status

# List detections
curl "http://localhost:8000/cctv_dev/detections?type=strongroom"
```

---

## 📝 Notes

1. **GPU Memory**: The HLS worker uses GPU batch processing. Adjust `GPU_BATCH_SIZE` env var if needed.
2. **Thread Safety**: Uses `_state_lock` for thread-safe state management.
3. **Graceful Shutdown**: Supports SIGTERM/SIGINT for clean shutdown.
4. **Scene Filtering**: Only processes cameras with matching classification to reduce unnecessary processing.
5. **DB Reporting**: Automatically reports detections to the database for client dashboard integration.

---

## 🎉 Status: READY FOR PRODUCTION

All integration points have been completed and tested. The Strong Room Movement detection is now fully operational and follows the same architecture patterns as other detection scripts in the system.
