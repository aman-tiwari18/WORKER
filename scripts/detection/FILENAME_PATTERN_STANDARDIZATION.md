# Detection Filename Pattern Standardization

## Summary
All detection scripts now follow a consistent filename pattern for saved images.

## Filename Patterns

### Core Detection Scripts (RTSP/Standalone)
**Pattern:**
```python
fname = f"{script_name}_{device_id}_{camera_no}_{ts}_{uid}.jpg"
filename = os.path.join(SAVE_FOLDER, fname)
```

**Example:**
```
proximity_CAM001_1_20260503_143022_a1b2c3d4-e5f6-7890-abcd-ef1234567890.jpg
```

### Worker Scripts

#### HLS Pattern:
```python
fname = f"{script_name}_ch{ch_no}_exam{exam_id}_{ts}_{uid}.jpg"
```

**Example:**
```
proximity_ch54703_exam123_20260503_143022_a1b2c3d4-e5f6-7890-abcd-ef1234567890.jpg
```

#### RTSP Pattern:
```python
fname = f"{script_name}_{dev_id}_{ch_no}_exam{exam_id}_{ts}_{uid}.jpg"
```

**Example:**
```
proximity_CAM001_1_exam123_20260503_143022_a1b2c3d4-e5f6-7890-abcd-ef1234567890.jpg
```

## Updated Files

### ✅ Core Detection Scripts (All Updated)
1. **close_proximity.py** - ✅ Reference implementation
2. **crowd_five_det.py** - ✅ Updated
3. **crowd_zero_det.py** - ✅ Updated
4. **Crowd_det_complete.py** - ✅ Updated
5. **Crowd_det_biometric.py** - ✅ Updated
6. **qp_box_det.py** - ✅ Updated
7. **invigilator_tracking.py** - ✅ Updated
8. **StrongRoom_det.py** - ✅ Updated (added device_id)
9. **Server_room_det.py** - ✅ Updated (fixed path format)
10. **phone_det_complete.py** - ✅ Updated (added device_id + uid)
11. **Intruder_det.py** - ✅ Updated (added device_id + uid)
12. **Entry_exit_det.py** - ✅ Updated (added device_id + uid)

### ✅ Worker Scripts (Already Correct)
All worker scripts already follow the correct dual pattern (HLS vs RTSP):
- crowd_worker.py
- crowd_five_det_worker.py
- crowd_zero_det_worker.py
- crowd_det_biometric_worker.py
- close_proximity_worker.py
- intruder_worker.py
- invigilator_worker.py
- phone_worker.py
- entry_exit_worker.py
- server_room_worker.py
- strongroom_worker.py
- qp_box_det_worker.py
- blind_spot_worker.py
- camera_inversion_worker.py
- camera_tampering_worker.py

## Key Changes Made

### 1. Added UUID to All Filenames
**Before:**
```python
filename = f"{SAVE_FOLDER}/{script_name}_{camera_no}_{ts}.jpg"
```

**After:**
```python
uid = str(uuid.uuid4())
fname = f"{script_name}_{device_id}_{camera_no}_{ts}_{uid}.jpg"
filename = os.path.join(SAVE_FOLDER, fname)
```

### 2. Added device_id to Filenames
**Before:**
```python
filename = f"{script_name}_{camera_no}_{ts}_{uid}.jpg"  # Missing device_id
```

**After:**
```python
fname = f"{script_name}_{device_id}_{camera_no}_{ts}_{uid}.jpg"  # Includes device_id
```

### 3. Fixed Path Construction
**Before:**
```python
filename = f"{SAVE_FOLDER}/{script_name}_{device_id}_{camera_no}_{ts}_{uid}.jpg"
```

**After:**
```python
fname = f"{script_name}_{device_id}_{camera_no}_{ts}_{uid}.jpg"
filename = os.path.join(SAVE_FOLDER, fname)
```

### 4. Added Database Reporting
All core detection scripts now report to database:
```python
try:
    from utils.db_client import report_to_db_rtsp
    report_to_db_rtsp(
        device_id=str(device_id),
        camera_no=str(camera_no),
        image_name=fname,
        folder="detection_folder_name",
    )
except Exception as e:
    logger.error(f"[{device_id}-{camera_no}] DB reporting error: {e}")
```

## Filename Components

| Component | Description | Example |
|-----------|-------------|---------|
| `script_name` | Detection type | `proximity`, `crowd`, `phone` |
| `device_id` | Camera device ID (RTSP) | `CAM001`, `NVR123` |
| `ch_no` / `camera_no` | Channel number | `1`, `54703` |
| `exam_id` | Exam ID (workers only) | `123`, `456` |
| `ts` | Timestamp | `20260503_143022` |
| `uid` | Unique identifier (UUID) | `a1b2c3d4-e5f6-7890-abcd-ef1234567890` |

## Benefits

1. **Unique Filenames**: UUID prevents overwrites
2. **Consistent Format**: All scripts follow same pattern
3. **Easy Parsing**: main.py can reliably extract metadata
4. **Database Integration**: All detections are tracked
5. **Source Identification**: HLS vs RTSP easily distinguished
6. **Exam Tracking**: Workers include exam_id for better organization

## Verification

To verify the pattern is working:

```bash
# Check RTSP detections
ls script-server/scripts/detection/results/proximity_detection/

# Check HLS detections
ls script-server/scripts/detection/results/tcs_detections/proximity_detection/

# Expected format:
# RTSP: proximity_CAM001_1_20260503_143022_uuid.jpg
# HLS:  proximity_ch54703_exam123_20260503_143022_uuid.jpg
```

## main.py Compatibility

The `main.py` parser now correctly handles both patterns:

```python
# RTSP pattern detection
if _is_rtsp_filename(stem):
    parts = stem.split("_")
    rtsp_device_id = parts[1]
    rtsp_camera_no = parts[2]
    # ... extract metadata

# HLS pattern detection
else:
    ch_no = _extract_ch_no(camera_id)
    # ... extract metadata
```

All detection images are now properly indexed and displayed in the frontend! 🎉
