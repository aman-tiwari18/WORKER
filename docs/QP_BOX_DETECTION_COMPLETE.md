# QP Box Detection Implementation - Complete

## Overview
Created detection scripts for identifying when Question Paper (QP) boxes are NOT visible in strong room cameras. The system detects white, grey, and brown colored boxes and alerts when they are missing.

## Files Created

### 1. HLS Worker Script
**File**: `script-server/scripts/detection/workers/qp_box_det_worker.py`
- **Architecture**: GPU-batched YOLO worker with per-camera feeder threads
- **Purpose**: Processes HLS streaming links for QP box detection
- **Features**:
  - Single GPU worker with shared queue for batch inference
  - Color-based detection (white, grey, brown) using HSV color ranges
  - Size filtering (5,000 - 200,000 pixels)
  - YOLO object detection for box-like objects (classes: 24, 25, 26, 28, 73, 84)
  - 240-second cooldown between alerts per camera
  - 25 detections/minute rate limiting
  - DB reporting integration for HLS cameras
  - Saves to `results/tcs_detections/qp_box_missing/`

### 2. RTSP Standalone Script
**File**: `script-server/scripts/detection/qp_box_det.py`
- **Architecture**: Standalone RTSP processing with threading
- **Purpose**: Processes RTSP streaming links for QP box detection
- **Features**:
  - Multi-backend support (FFmpeg, GStreamer, V4L2)
  - Same detection logic as HLS worker
  - Color-based detection (white, grey, brown)
  - Size filtering (5,000 - 200,000 pixels)
  - 240-second cooldown between alerts
  - DB reporting integration for RTSP cameras
  - **Database integration** - fetches cameras from `camera_links_rtsp` table
  - Parallel processing with threading
  - Saves to `results/tcs_detections/qp_box_missing/`

## Detection Logic

### Color Detection (HSV Ranges)
```python
COLOR_RANGES = {
    'white': {
        'lower': [0, 0, 180],
        'upper': [180, 30, 255]
    },
    'grey': {
        'lower': [0, 0, 80],
        'upper': [180, 50, 180]
    },
    'brown': {
        'lower': [10, 50, 50],
        'upper': [20, 255, 200]
    }
}
```

### Detection Process
1. **YOLO Object Detection**: Identifies potential box-like objects
   - Classes: backpack (24), umbrella (25), handbag (26), suitcase (28), book (73, 84)
   - Confidence threshold: 0.45

2. **Size Filtering**: Checks if detected object is appropriate size
   - Minimum area: 5,000 pixels
   - Maximum area: 200,000 pixels

3. **Color Matching**: Verifies if object matches QP box colors
   - Converts ROI to HSV color space
   - Checks against white/grey/brown color ranges
   - Requires >30% color match ratio

4. **Alert Generation**: Triggers when QP box is NOT found
   - Saves annotated frame with warning
   - Reports to database
   - Applies cooldown and rate limiting

## Target Scenes
- **Server Room**
- **Strong Room**

Only cameras classified with these scenes are monitored for QP box presence.

## Configuration

### Thresholds
- **COOLDOWN_TIME**: 240 seconds (4 minutes between saves per camera)
- **CONFIDENCE**: 0.45 (YOLO confidence threshold)
- **MIN_BOX_AREA**: 5,000 pixels
- **MAX_BOX_AREA**: 200,000 pixels
- **COLOR_MATCH_RATIO**: 0.3 (30% of box must match target color)

### Rate Limiting
- **Max detections per minute**: 25 (global limit)
- Prevents alert flooding
- Shared across all cameras

## Database Integration

### HLS Cameras
- Reports to `camera_report_hls` table
- Uses `report_to_db_hls()` function
- Requires `EXAM_ID` and `SHIFT_ID` environment variables

### RTSP Cameras
- Reports to RTSP reporting table
- Uses `report_to_db_rtsp()` function
- Includes device_id and camera_no

## Output

### Saved Images
- **Location**: `results/tcs_detections/qp_box_missing/`
- **Naming Format**:
  - HLS: `qp_box_missing_ch{ch_no}_exam{exam_id}_{timestamp}_{uuid}.jpg`
  - RTSP: `qp_box_missing_{device_id}_{camera_no}_{timestamp}.jpg`
- **Annotations**:
  - "QP BOX NOT DETECTED!" warning text
  - Camera identification
  - Timestamp

## Usage

### HLS Worker (via master_pipeline)
```python
from workers.qp_box_det_worker import qp_box_det_worker_hls

# Single camera
qp_box_det_worker_hls(camera_config)

# Multiple cameras
qp_box_det_worker_hls(camera_configs_list)
```

### RTSP Standalone
```bash
cd script-server/scripts/detection
python qp_box_det.py
```

## Environment Variables

### Required
- `EXAM_ID`: Exam identifier for DB queries (both HLS and RTSP)

### Optional (HLS)
- `SHIFT_ID`: Shift identifier for HLS cameras
- `ASSIGNMENT_DATE`: Date for camera assignment (default: today)
- `LINK_TYPE`: "HLS" or "RTSP" (default: "HLS")
- `TCS_QP_BOX_SAMPLE_EVERY`: Frame sampling rate (default: 10)
- `TCS_QP_BOX_BATCH_SIZE`: GPU batch size (default: 64)
- `TCS_QP_BOX_QUEUE_SIZE`: Queue size (default: 128)
- `TCS_QP_BOX_THREAD_DELAY`: Delay between thread starts (default: 0.1s)

### Optional (Database)
- `DB_HOST`: Database host (default: "localhost")
- `DB_PORT`: Database port (default: 3306)
- `DB_USER`: Database user (default: "root")
- `DB_PASSWORD`: Database password (default: "rootpassword123")
- `DB_NAME`: Database name (default: "cctv_monitoring")

## Architecture Patterns

### HLS Worker
- **Pattern**: GPU-batched worker with feeder threads
- **Reference**: `crowd_zero_det_worker.py`
- **Benefits**:
  - Efficient GPU utilization
  - True batch inference
  - Scalable to many cameras
  - Shared resources

### RTSP Standalone
- **Pattern**: Multi-threaded RTSP processing
- **Reference**: `crowd_zero_det.py`
- **Benefits**:
  - Independent camera processing
  - Multi-backend support
  - Database integration
  - Parallel execution

## Next Steps

### 1. Testing
- Test with actual strong room footage
- Verify color detection accuracy
- Adjust color ranges if needed
- Validate size thresholds

### 2. Master Pipeline Registration
- Add QP box detection to pipeline registry
- Configure scheduling
- Set up monitoring

### 3. Fine-tuning
- Adjust color ranges based on real QP boxes
- Optimize size thresholds
- Consider adding shape detection (rectangular)
- Tune confidence threshold

### 4. Monitoring
- Track false positive rate
- Monitor detection accuracy
- Review saved images
- Adjust parameters as needed

## Technical Notes

### Color Detection Challenges
- Lighting conditions affect HSV values
- May need to adjust ranges for different environments
- Consider adding adaptive thresholding

### Size Filtering
- Current range (5K-200K pixels) is estimated
- May need adjustment based on camera distance
- Consider camera-specific thresholds

### YOLO Classes
- Using generic object classes as proxies for boxes
- May benefit from custom-trained model
- Current classes: backpack, handbag, suitcase, book

## Troubleshooting

### No Detections
1. Check if cameras are in target scenes
2. Verify YOLO model is loaded
3. Check color ranges match actual QP boxes
4. Verify size thresholds are appropriate

### Too Many False Positives
1. Increase confidence threshold
2. Narrow color ranges
3. Adjust size filtering
4. Add shape detection

### Performance Issues
1. Reduce batch size
2. Increase frame sampling interval
3. Reduce number of concurrent cameras
4. Check GPU memory usage

## Status
✅ **COMPLETE** - Both HLS worker and RTSP standalone scripts created and ready for testing.
