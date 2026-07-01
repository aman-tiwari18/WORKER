# Camera Classification Categories Update

## Summary
Updated all detection worker scripts to filter cameras based on the new classification categories as per the analytics requirements table. Created separate crowd detection scripts for Entry/Exit and Biometric areas.

## Camera Type Mappings

### 1. **Strong Room** Cameras
**Detection Scripts:**
- ✅ QP Box Detection (`qp_box_det_worker.py`)
- ✅ Intruder Detection (`intruder_worker.py`)

**Classification Keywords:** `strong room`, `strongroom`, `strong_room`

---

### 2. **Entry/Exit** Cameras
**Detection Scripts:**
- ✅ Crowd Detection (`crowd_worker.py`) - For Entry/Exit gates
- ✅ Entry/Exit Movement Detection (`entry_exit_worker.py`)

**Classification Keywords:** `entry/exit`, `entry_exit`, `entryexit`

---

### 3. **Biometric** Cameras
**Detection Scripts:**
- ✅ Crowd Detection Biometric (`crowd_det_biometric_worker.py`) - **NEW** Separate script for biometric area

**Classification Keywords:** `biometric`

---

### 4. **Inside Classroom** Cameras
**Detection Scripts:**
- ✅ Invigilator Not Moving (`invigilator_worker.py`)
- ✅ Two or More People in Close Proximity (`close_proximity_worker.py`)
- ✅ Mobile Phone Detection (`phone_worker.py`)
- ✅ Candidate Count Less Than 5 (`crowd_zero_det_worker.py`)
- ✅ Crowd Detection Threshold (`crowd_five_det_worker.py`)

**Classification Keywords:** `inside classroom`, `inside_classroom`, `classroom`

---

## New Files Created

### 1. **Biometric Crowd Detection Core Module**
**File:** `script-server/scripts/detection/Crowd_det_biometric.py`
- Dedicated crowd detection logic for biometric areas
- Uses same YOLO model but separate configuration
- Threshold: 12+ persons for crowd detection
- Separate save folder: `results/crowd_detection_biometric`

### 2. **Biometric Crowd Detection Worker**
**File:** `script-server/scripts/detection/workers/crowd_det_biometric_worker.py`
- Worker implementation for biometric crowd detection
- Supports both RTSP and HLS streams
- Filters only cameras with `biometric` classification
- Function names:
  - RTSP: `crowd_det_biometric_worker(frame_queue)`
  - HLS: `crowd_det_biometric_worker_hls(camera_configs, stop_event=None)`

---

## Files Modified

1. **script-server/scripts/detection/workers/qp_box_det_worker.py**
   - Updated filtering to only process Strong Room cameras
   - Changed from: `('strongroom', 'vault', 'locker')`
   - Changed to: `('strong room', 'strongroom', 'strong_room')`

2. **script-server/scripts/detection/workers/intruder_worker.py**
   - Updated filtering to only process Strong Room cameras
   - Changed from: `('corridor', 'entrance', 'lobby', 'parking')`
   - Changed to: `('strong room', 'strongroom', 'strong_room')`

3. **script-server/scripts/detection/workers/crowd_worker.py** ⚠️ **UPDATED**
   - Updated filtering to process **Entry/Exit cameras ONLY** (removed biometric)
   - Changed from: `('corridor', 'entrance', 'lobby', 'parking', 'classroom', 'exam')`
   - Changed to: `('entry/exit', 'entry_exit', 'entryexit')`
   - Biometric cameras now handled by separate worker

4. **script-server/scripts/detection/workers/entry_exit_worker.py**
   - Updated filtering to only process Entry/Exit cameras
   - Changed from: `('corridor', 'entrance', 'lobby', 'parking')`
   - Changed to: `('entry/exit', 'entry_exit', 'entryexit')`

5. **script-server/scripts/detection/workers/invigilator_worker.py**
   - Updated filtering to only process Inside Classroom cameras
   - Changed from: `('classroom', 'exam', 'hall')`
   - Changed to: `('inside classroom', 'inside_classroom', 'classroom')`

6. **script-server/scripts/detection/workers/phone_worker.py**
   - Updated filtering to only process Inside Classroom cameras
   - Changed from: `('classroom', 'exam', 'lab', 'corridor')`
   - Changed to: `('inside classroom', 'inside_classroom', 'classroom')`

7. **script-server/scripts/detection/workers/close_proximity_worker.py**
   - Updated filtering to only process Inside Classroom cameras
   - Changed from: `('corridor', 'entrance', 'lobby', 'parking', 'classroom', 'exam')`
   - Changed to: `('inside classroom', 'inside_classroom', 'classroom')`

8. **script-server/scripts/detection/workers/crowd_zero_det_worker.py**
   - Updated filtering to only process Inside Classroom cameras
   - Changed from: `('corridor', 'entrance', 'lobby', 'parking')`
   - Changed to: `('inside classroom', 'inside_classroom', 'classroom')`

9. **script-server/scripts/detection/workers/crowd_five_det_worker.py**
   - Updated filtering to only process Inside Classroom cameras
   - Changed from: `('corridor', 'entrance', 'lobby', 'parking')`
   - Changed to: `('inside classroom', 'inside_classroom', 'classroom')`

---

## Analytics Table Reference

| Sr No. | Analytics | Frequency | Camera Type | Time Window |
|--------|-----------|-----------|-------------|-------------|
| 1 | Intruder Detected inside Strong Room | 1 Minute | Strong Room | 07:00:00 - 13:30:00 |
| 2 | Crowd Detection - More than 10 people at entry gate | 10 Minutes / 5 Minutes | Entry/Exit | 11:00:00 - 14:00:00 |
| 3 | Crowd Detection - More than 10 people at Biometric area | 10 Minutes / 5 Minutes | Biometric | 11:00:00 - 14:00:00 |
| 4 | Invigilator Not Moving | 7 Minutes | Inside Classroom | 14:00:00 - 18:00:00 |
| 5 | Two or More People in Close Proximity | 2 Minutes | Inside Classroom | 14:00:00 - 18:00:00 |
| 6 | Movement at entry exit during examination | 2 Minutes | Entry/Exit | 14:00:00 - 18:00:00 |
| 7 | Candidate count less than 5 | - | Inside Classroom | 14:00:00 - 18:00:00 |
| 8 | Mobile Phone Detection in classroom | 1.5 Minutes | Inside Classroom | 14:00:00 - 18:00:00 |
| 9 | QP Box not visible in Strong Room | - | Strong Room | 07:00:00 - 13:30:00 |
| 10 | No camera in strong room | - | Strong Room | At preprocessing |
| 11 | Validation of camera labels/Names via vis location of camera | - | - | At preprocessing |
| 12 | Reporting of inaccessible/offline cameras | - | - | At preprocessing |

---

## Script Mapping

### Crowd Detection Scripts (Separated)

1. **Entry/Exit Crowd Detection**
   - Core Module: `Crowd_det_complete.py`
   - Worker: `crowd_worker.py`
   - Function: `crowd_worker()` / `crowd_worker_hls()`
   - Classification: Entry/Exit cameras only
   - Folder: `results/crowd_detection`

2. **Biometric Crowd Detection** 🆕
   - Core Module: `Crowd_det_biometric.py` (NEW)
   - Worker: `crowd_det_biometric_worker.py` (NEW)
   - Function: `crowd_det_biometric_worker()` / `crowd_det_biometric_worker_hls()`
   - Classification: Biometric cameras only
   - Folder: `results/crowd_detection_biometric`

---

## Testing Recommendations

1. **Verify Camera Classification Field:**
   - Ensure database has correct `classification` field values
   - Check that values match the keywords: `Strong Room`, `Entry/Exit`, `Biometric`, `Inside Classroom`

2. **Test Each Detection Type:**
   - Strong Room: Test QP Box and Intruder detection
   - Entry/Exit: Test Crowd and Movement detection
   - Biometric: Test Biometric Crowd detection (NEW separate script)
   - Inside Classroom: Test all 5 detection types

3. **Check Filtering Logic:**
   - Verify cameras with empty classification still get processed (fallback behavior)
   - Verify cameras with wrong classification are filtered out
   - **Important:** Verify biometric cameras are NOT processed by `crowd_worker.py`
   - **Important:** Verify biometric cameras ARE processed by `crowd_det_biometric_worker.py`

4. **Monitor Logs:**
   - Check for "Scene filtering" messages in worker logs
   - Verify correct number of cameras being processed per detection type
   - Look for `[CrowdBiometric]` log prefix for biometric detections
   - Look for `[Crowd]` log prefix for entry/exit detections

5. **Database Reporting:**
   - Verify biometric detections save to `crowd_detection_biometric` folder
   - Verify entry/exit detections save to `crowd_detection` folder
   - Check database entries have correct folder references

---

## Integration Notes

### For Master Pipeline Integration:
If using master_pipeline.py, you may need to register the new biometric worker:

```python
# In master_pipeline.py or equivalent
from workers.crowd_det_biometric_worker import crowd_det_biometric_worker

# Add to worker registry
WORKERS = {
    # ... existing workers ...
    'crowd_biometric': crowd_det_biometric_worker,
    # ... other workers ...
}
```

### For HLS Standalone Execution:
```python
from workers.crowd_det_biometric_worker import crowd_det_biometric_worker_hls

# Filter biometric cameras
biometric_cameras = [cam for cam in all_cameras 
                     if 'biometric' in cam.get('classification', '').lower()]

# Run worker
crowd_det_biometric_worker_hls(biometric_cameras)
```

---

## Notes

- All workers maintain backward compatibility with empty classification values
- Case-insensitive matching is used for classification keywords
- Multiple keyword variations are supported for flexibility (e.g., `entry/exit`, `entry_exit`, `entryexit`)
- RTSP workers don't have filtering (handled by master_pipeline)
- HLS workers have the filtering logic in their `*_worker_hls()` functions
- **Biometric and Entry/Exit crowd detections are now completely separate** to allow independent configuration and monitoring
